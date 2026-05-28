"""Tests for the dispatch-resilience features — preemption signal trap +
idempotency-skip on resubmit.

The campus user's low-priority jobs on a shared HPC are routinely
preempted by higher-priority work. These tests cover the two
mechanisms that help such jobs survive the cluster's preemption
window:

1. SIGTERM trap that marks the run as bumped (not failed) in the
   sidecar and exits 130.
2. Idempotency skip that lets a resubmitted preempted task exit 0
   without redoing already-completed work.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hpc_agent.models.mapreduce import dispatch
from tests.conftest import make_sidecar_json, write_hpc_tasks


def _scaffold(
    tmp_path: Path,
    *,
    executor: str,
    result_dir_template: str,
    kwargs_per_task: list[dict],
    run_id: str = "test_run",
) -> Path:
    hpc = tmp_path / ".hpc"
    write_hpc_tasks(hpc, kwargs_per_task)
    make_sidecar_json(
        tmp_path,
        run_id=run_id,
        executor=executor,
        result_dir_template=result_dir_template,
        task_count=len(kwargs_per_task),
        tasks_py_sha="abc123",
    )
    return hpc


# ---------------------------------------------------------------------------
# Idempotency skip
# ---------------------------------------------------------------------------


class TestIdempotencySkip:
    """A resubmitted preempted task whose ``metrics.json`` already
    exists must exit 0 without re-running the executor. The combiner
    picks up the existing output on the next wave."""

    def test_skips_when_metrics_json_present(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        # Sentinel file: if the executor runs, it will create this.
        sentinel = tmp_path / "executor_ran.flag"
        hpc = _scaffold(
            tmp_path,
            executor=f'touch "{sentinel}"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        # Pre-create a non-empty metrics.json — simulating a prior
        # completed run for the same task.
        task_dir = result_root / "0"
        task_dir.mkdir(parents=True)
        (task_dir / "metrics.json").write_text("{}")

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        # Executor must not have run.
        assert not sentinel.exists()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="pre-existing Windows platform failure (Unix-only stdlib or shell)",
    )
    def test_does_not_skip_on_zero_byte_metrics_json(self, tmp_path, monkeypatch):
        """A 0-byte metrics.json (e.g. crashed mid-write) must NOT
        lock the user out of re-running."""
        result_root = tmp_path / "results"
        sentinel = tmp_path / "executor_ran.flag"
        hpc = _scaffold(
            tmp_path,
            executor=f'touch "{sentinel}"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        task_dir = result_root / "0"
        task_dir.mkdir(parents=True)
        (task_dir / "metrics.json").write_text("")  # 0 bytes

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        # Executor MUST have run.
        assert sentinel.exists()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="pre-existing Windows platform failure (Unix-only stdlib or shell)",
    )
    def test_does_not_skip_when_result_dir_missing(self, tmp_path, monkeypatch):
        """No prior result_dir means a fresh task — executor runs."""
        result_root = tmp_path / "results"
        sentinel = tmp_path / "executor_ran.flag"
        hpc = _scaffold(
            tmp_path,
            executor=f'touch "{sentinel}"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        # Note: no result_root pre-creation.
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert sentinel.exists()


# ---------------------------------------------------------------------------
# SIGTERM trap
#
# These tests spawn the dispatcher as a real subprocess so we can send
# a real SIGTERM to it (the in-process signal-handler approach in
# pytest is fragile — pytest's own handlers interfere).
# ---------------------------------------------------------------------------


def _write_runner(
    runner_path: Path,
    *,
    hpc_dir: Path,
    repo_root: Path,
) -> None:
    """Materialize a Python script that imports dispatch and calls main(),
    matching how the deployed _hpc_dispatch.py runs on the cluster."""
    runner_path.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(repo_root)!r})
            from hpc_agent.models.mapreduce import dispatch
            # Make dispatch.__file__ resolve sidecars relative to .hpc/
            dispatch.__file__ = {str(hpc_dir / "_hpc_dispatch.py")!r}
            dispatch.main()
            """
        ).lstrip()
    )


@pytest.mark.slow
@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals only")
class TestPreemptionSignalTrap:
    def test_sigterm_writes_preempted_at_to_sidecar(self, tmp_path):
        """A SIGTERM during executor runtime must populate
        ``preempt: {at, grace_sec}`` on the per-task sidecar entry."""
        result_root = tmp_path / "results"
        # Long-running executor — sleeps until killed.
        hpc = _scaffold(
            tmp_path,
            executor="sleep 30",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        runner = tmp_path / "run.py"
        repo_root = Path(__file__).resolve().parent.parent.parent
        _write_runner(runner, hpc_dir=hpc, repo_root=repo_root)

        # Redirect stdout/stderr to files so the test can read them
        # after proc.wait() — using PIPE here would stall because the
        # executor subprocess inherits the pipes and outlives the
        # dispatcher's exit.
        stderr_log = tmp_path / "dispatcher.stderr"
        stdout_log = tmp_path / "dispatcher.stdout"

        env = dict(os.environ)
        env["HPC_TASK_ID"] = "0"
        env["HPC_RUN_ID"] = "test_run"
        env["HPC_TASKS_PATH"] = str(hpc / "tasks.py")
        # Short grace so the test exits quickly once SIGINT is forwarded.
        env["HPC_PREEMPT_GRACE_SEC"] = "2"

        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                env=env,
                stdout=out_fh,
                stderr=err_fh,
            )
            # Give the dispatcher time to install the handler and spawn
            # the child sleep before we send SIGTERM.
            time.sleep(2.0)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("dispatcher did not exit within 10s of SIGTERM")

        stderr_bytes = stderr_log.read_bytes()

        # Exit code must be 130 (POSIX preempted).
        assert proc.returncode == 130, (
            f"expected exit 130, got {proc.returncode}\nstderr: {stderr_bytes!r}"
        )

        # Stderr must announce preemption.
        assert b"preemption imminent" in stderr_bytes, stderr_bytes

        # Sidecar must have preempt block populated for task 0.
        sidecar = json.loads((hpc / "runs" / "test_run.json").read_text())
        tasks = sidecar.get("tasks") or {}
        entry = tasks.get("0") or {}
        assert "preempt" in entry, f"sidecar missing preempt for task 0; got {sidecar}"
        preempt = entry["preempt"]
        assert isinstance(preempt, dict), preempt
        assert "at" in preempt and "grace_sec" in preempt, preempt
        # ISO-8601 with Z suffix.
        assert preempt["at"].endswith("Z"), preempt["at"]
        # grace_sec round-trips the env override (set to 2 above).
        assert preempt["grace_sec"] == 2, preempt

    def test_grace_sec_env_override_is_honored(self, tmp_path):
        """Setting HPC_PREEMPT_GRACE_SEC=1 forces a quick teardown
        even if the executor would otherwise sleep longer."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            # Executor traps SIGINT and keeps running for 20s; we
            # expect the dispatcher to exit after only ~1s grace
            # regardless.
            executor="trap '' INT; sleep 20",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        runner = tmp_path / "run.py"
        repo_root = Path(__file__).resolve().parent.parent.parent
        _write_runner(runner, hpc_dir=hpc, repo_root=repo_root)

        env = dict(os.environ)
        env["HPC_TASK_ID"] = "0"
        env["HPC_RUN_ID"] = "test_run"
        env["HPC_TASKS_PATH"] = str(hpc / "tasks.py")
        env["HPC_PREEMPT_GRACE_SEC"] = "1"

        stdout_log = tmp_path / "dispatcher.stdout"
        stderr_log = tmp_path / "dispatcher.stderr"
        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                env=env,
                stdout=out_fh,
                stderr=err_fh,
            )
            time.sleep(2.0)
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("dispatcher did not honor HPC_PREEMPT_GRACE_SEC=1")
            elapsed = time.monotonic() - t0

        # Exit must be 130 and within ~1s grace + small slack.
        assert proc.returncode == 130
        assert elapsed < 6.0, f"teardown took {elapsed:.2f}s; expected ~1s"

    def test_repeated_sigterm_is_ignored_after_first(self, tmp_path):
        """A-H3: re-entrancy guard. A second SIGTERM mid-handler must
        be a no-op — the campus user can't debug a recursive sys.exit
        from a cluster log."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="sleep 30",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        runner = tmp_path / "run.py"
        repo_root = Path(__file__).resolve().parent.parent.parent
        _write_runner(runner, hpc_dir=hpc, repo_root=repo_root)

        env = dict(os.environ)
        env["HPC_TASK_ID"] = "0"
        env["HPC_RUN_ID"] = "test_run"
        env["HPC_TASKS_PATH"] = str(hpc / "tasks.py")
        env["HPC_PREEMPT_GRACE_SEC"] = "3"

        stdout_log = tmp_path / "dispatcher.stdout"
        stderr_log = tmp_path / "dispatcher.stderr"
        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                env=env,
                stdout=out_fh,
                stderr=err_fh,
            )
            time.sleep(2.0)
            # Burst of SIGTERMs — only the first should fire the
            # handler; the rest must be silently ignored.
            for _ in range(5):
                proc.send_signal(signal.SIGTERM)
                time.sleep(0.05)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("dispatcher did not exit cleanly after burst SIGTERM")

        stderr_bytes = stderr_log.read_bytes()

        # Exit code must still be 130; no recursive double-exit.
        assert proc.returncode == 130, (
            f"expected exit 130, got {proc.returncode}\nstderr: {stderr_bytes!r}"
        )
        # The "preemption imminent" stderr line should appear exactly
        # once — re-entry into the handler would print it again.
        assert stderr_bytes.count(b"preemption imminent") == 1, stderr_bytes

    def test_grace_sec_zero_is_accepted_and_skips_wait_loop(self, tmp_path):
        """A-M5: HPC_PREEMPT_GRACE_SEC=0 must not crash; documented
        intent is 'no grace, exit immediately'."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="sleep 30",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        runner = tmp_path / "run.py"
        repo_root = Path(__file__).resolve().parent.parent.parent
        _write_runner(runner, hpc_dir=hpc, repo_root=repo_root)

        env = dict(os.environ)
        env["HPC_TASK_ID"] = "0"
        env["HPC_RUN_ID"] = "test_run"
        env["HPC_TASKS_PATH"] = str(hpc / "tasks.py")
        env["HPC_PREEMPT_GRACE_SEC"] = "0"

        stdout_log = tmp_path / "dispatcher.stdout"
        stderr_log = tmp_path / "dispatcher.stderr"
        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                env=env,
                stdout=out_fh,
                stderr=err_fh,
            )
            time.sleep(2.0)
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("HPC_PREEMPT_GRACE_SEC=0 did not exit promptly")
            elapsed = time.monotonic() - t0

        assert proc.returncode == 130
        # Zero grace should land in well under 5s (terminate→wait(2)→kill).
        assert elapsed < 5.0, f"teardown with grace=0 took {elapsed:.2f}s"

        # Sidecar still records grace_sec=0 (round-tripped from env).
        sidecar = json.loads((hpc / "runs" / "test_run.json").read_text())
        entry = sidecar["tasks"]["0"]
        assert entry["preempt"]["grace_sec"] == 0

    def test_grace_sec_invalid_falls_back_to_default(self, tmp_path):
        """A non-integer HPC_PREEMPT_GRACE_SEC must not crash dispatch;
        survival over strictness — fall back to the documented default."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="echo ok",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        runner = tmp_path / "run.py"
        repo_root = Path(__file__).resolve().parent.parent.parent
        _write_runner(runner, hpc_dir=hpc, repo_root=repo_root)

        env = dict(os.environ)
        env["HPC_TASK_ID"] = "0"
        env["HPC_RUN_ID"] = "test_run"
        env["HPC_TASKS_PATH"] = str(hpc / "tasks.py")
        env["HPC_PREEMPT_GRACE_SEC"] = "abc"

        stdout_log = tmp_path / "dispatcher.stdout"
        stderr_log = tmp_path / "dispatcher.stderr"
        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                env=env,
                stdout=out_fh,
                stderr=err_fh,
            )
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("dispatcher hung on bad HPC_PREEMPT_GRACE_SEC")

        # Executor printed "ok" → exit 0; no crash from int("abc").
        assert proc.returncode == 0, stderr_log.read_bytes()

    def test_kill_escalation_when_executor_ignores_sigint_and_sigterm(self, tmp_path):
        """A-H2: if the executor ignores both SIGINT (forwarded) and
        SIGTERM (escalated), the dispatcher must SIGKILL it before
        exiting. A zombie executor outliving the dispatcher would
        keep writing to the next user's half-rotated log."""
        result_root = tmp_path / "results"
        # Trap both INT and TERM; survive only SIGKILL.
        hpc = _scaffold(
            tmp_path,
            executor="trap '' INT TERM; sleep 60",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        runner = tmp_path / "run.py"
        repo_root = Path(__file__).resolve().parent.parent.parent
        _write_runner(runner, hpc_dir=hpc, repo_root=repo_root)

        env = dict(os.environ)
        env["HPC_TASK_ID"] = "0"
        env["HPC_RUN_ID"] = "test_run"
        env["HPC_TASKS_PATH"] = str(hpc / "tasks.py")
        env["HPC_PREEMPT_GRACE_SEC"] = "1"

        stdout_log = tmp_path / "dispatcher.stdout"
        stderr_log = tmp_path / "dispatcher.stderr"
        with open(stdout_log, "wb") as out_fh, open(stderr_log, "wb") as err_fh:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                env=env,
                stdout=out_fh,
                stderr=err_fh,
            )
            time.sleep(2.0)
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("dispatcher did not escalate to SIGKILL")
            elapsed = time.monotonic() - t0

        assert proc.returncode == 130
        # 1s grace + ≤2s terminate-wait + ≤2s kill-wait + slack.
        assert elapsed < 8.0, f"escalation took {elapsed:.2f}s"
