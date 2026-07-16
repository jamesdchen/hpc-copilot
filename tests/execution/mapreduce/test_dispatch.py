"""Tests for hpc_agent.execution.mapreduce.dispatch — the cluster-side framework executor.

The dispatcher imports the user's ``.hpc/tasks.py``, reads the per-run
sidecar at ``.hpc/runs/<run_id>.json`` for the executor command and
result_dir template, formats result_dir from kwargs, and runs the
executor with WIP / atomic-promote semantics.
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING

import pytest

from hpc_agent.execution.mapreduce import dispatch
from hpc_agent.execution.mapreduce.reduce.status import check_results
from tests.conftest import make_sidecar_json, write_hpc_tasks

if TYPE_CHECKING:
    from pathlib import Path

# The dispatcher launches the per-task executor via subprocess with
# shell=True — that's /bin/sh on the cluster (the only place it runs). Tests
# that actually spawn an executor feed POSIX-shell command strings ($VAR,
# &&, redirection), which Windows cmd.exe can't interpret. The dispatcher
# itself is platform-correct; only these spawn-path tests are POSIX-bound.
# (Tests that exit before spawning — validation, idempotency skips — run
# everywhere and are deliberately left unmarked.) See #163.
_posix_shell_executor = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executor runs through shell=True (cmd.exe on Windows); POSIX-shell "
    "executor strings need /bin/sh as on the cluster (#163)",
)


def _scaffold(
    tmp_path: Path,
    *,
    executor: str,
    result_dir_template: str,
    kwargs_per_task: list[dict],
    run_id: str = "test_run",
) -> Path:
    """Materialize a ``.hpc/`` next to *tmp_path* with tasks.py + sidecar.

    Returns the ``.hpc/`` path so callers can override env vars.
    """
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


@_posix_shell_executor
class TestDispatchAtomicOutput:
    def test_successful_task_promotes_output(self, tmp_path, monkeypatch):
        # The dispatcher uses HPC_TASKS_PATH override to find tasks.py
        # outside cwd; we point it at the .hpc/ we set up under tmp_path.
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo hello > "$RESULT_DIR/results_task_1.csv"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}],  # two tasks, kwargs empty
        )

        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        # Cluster-side executor uses sibling lookup of tasks.py to find runs/;
        # for tests we inject the .hpc dir via __file__ alongside tasks.py:
        monkeypatch.chdir(tmp_path)
        # Trick: dispatch.py uses Path(__file__).parent for tasks/sidecar;
        # but with HPC_TASKS_PATH set, tasks.py loads from override. The
        # sidecar lookup still uses Path(__file__).parent / "runs" / ...
        # In tests we have to point that at .hpc/. Patch the module's
        # __file__-derived path:
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0
        result_dir = result_root / "1"
        assert (result_dir / "results_task_1.csv").exists()
        assert (result_dir / "results_task_1.csv").read_text().strip() == "hello"
        assert not (result_dir / "_wip_1").exists()

    def test_failed_task_preserves_wip(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo partial > "$RESULT_DIR/out.csv" && exit 1',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 1
        wip_dir = result_root / "0" / "_wip_0"
        assert wip_dir.exists()
        assert (wip_dir / "out.csv").read_text().strip() == "partial"
        assert not (result_root / "0" / "out.csv").exists()


@_posix_shell_executor
class TestTerminalAnnouncement:
    """Crash-only Phase 1: the dispatcher announces its own per-task verdict.

    ONE filename-state-encoded marker per task under ``.hpc/announce/<run_id>/``,
    written on BOTH the success and failure terminal paths, atomically, and
    best-effort (a raising marker write never changes the task's exit code).
    """

    def test_success_writes_complete_marker(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo hello > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0
        announce_dir = hpc / "announce" / "test_run"
        complete_marker = announce_dir / "task_1.complete"
        assert complete_marker.exists()
        assert not (announce_dir / "task_1.failed").exists()
        payload = json.loads(complete_marker.read_text())
        assert payload["task_id"] == 1
        assert payload["state"] == "complete"
        assert payload["exit_code"] == 0
        assert payload["finished_at"]

    def test_failure_writes_failed_marker(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo partial > "$RESULT_DIR/out.csv" && exit 1',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 1
        announce_dir = hpc / "announce" / "test_run"
        failed_marker = announce_dir / "task_0.failed"
        assert failed_marker.exists()
        assert not (announce_dir / "task_0.complete").exists()
        payload = json.loads(failed_marker.read_text())
        assert payload["state"] == "failed"
        assert payload["exit_code"] == 1

    def test_empty_output_verdict_reflected_in_marker(self, tmp_path, monkeypatch):
        # finding-16: exit 0 but no output is REMAPPED to a failure. The marker
        # must mirror the promote/failure VERDICT, not the raw executor rc 0.
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="true",  # exits 0, writes nothing
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == dispatch._EXIT_NO_OUTPUT
        announce_dir = hpc / "announce" / "test_run"
        assert (announce_dir / "task_0.failed").exists()
        assert not (announce_dir / "task_0.complete").exists()
        payload = json.loads((announce_dir / "task_0.failed").read_text())
        assert payload["state"] == "failed"
        assert payload["exit_code"] == dispatch._EXIT_NO_OUTPUT

    def test_marker_write_failure_never_fails_task(self, tmp_path, monkeypatch):
        # Best-effort: a raising atomic-write must be swallowed — the task keeps
        # its own exit code, the announcement is merely lost.
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo hello > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        real_atomic = dispatch._atomic_write_json

        def _boom(path, data):
            # Only sabotage the announcement write (under announce/), never the
            # runtime/cmd_sha writes the success path also does.
            if "announce" in str(path):
                raise OSError("simulated marker write failure")
            return real_atomic(path, data)

        monkeypatch.setattr(dispatch, "_atomic_write_json", _boom)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        # Task still succeeds; marker simply absent.
        assert exc_info.value.code == 0
        assert not (hpc / "announce" / "test_run" / "task_0.complete").exists()


class TestSkipPathAnnouncements:
    """The two terminal-exit-0 SKIP paths must ALSO announce, or the announce
    census reads those tasks ``missing`` forever (missing->pending → the watch
    rides to TIMEOUT, auto-resume/recover unreachable).

    Both skips exit 0 BEFORE spawning any executor, so they are not
    shell-bound (no ``_posix_shell_executor`` mark).
    """

    def test_partial_repro_skip_announces_complete(self, tmp_path, monkeypatch):
        """F19: a task NOT in HPC_TASK_INCLUDE (partial reproduction) exits 0 by
        design — and must announce COMPLETE so the census closes and the partial
        reproduction settles, instead of the never-selected tasks reading missing
        forever."""
        hpc = _scaffold(
            tmp_path,
            executor='echo hello > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(tmp_path / "results" / "{task_id}"),
            kwargs_per_task=[{}, {}, {}, {}],
        )
        # Only task 0 is selected; task 1 is a non-selected index → skip + announce.
        monkeypatch.setenv("HPC_TASK_INCLUDE", "0")
        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0
        announce_dir = hpc / "announce" / "test_run"
        marker = announce_dir / "task_1.complete"
        assert marker.exists()
        assert not (announce_dir / "task_1.failed").exists()
        payload = json.loads(marker.read_text())
        assert payload["task_id"] == 1
        assert payload["state"] == "complete"

    def test_idempotency_skip_reannounces_complete(self, tmp_path, monkeypatch):
        """F17: a task whose summary artifact already exists (idempotency skip)
        exits 0 WITHOUT re-running — and must RE-announce COMPLETE so a task whose
        attempt-1 marker was lost/cleared does not read ``missing`` forever on the
        census after a resubmit."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo hello > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}],
        )
        # Pre-stage a non-empty metrics.json so the idempotency skip fires, and
        # NO announce marker (attempt-1's marker was lost/cleared).
        task_dir = result_root / "1"
        task_dir.mkdir(parents=True)
        (task_dir / "metrics.json").write_text("x", encoding="utf-8")

        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0
        marker = hpc / "announce" / "test_run" / "task_1.complete"
        assert marker.exists()
        payload = json.loads(marker.read_text())
        assert payload["task_id"] == 1
        assert payload["state"] == "complete"


class TestEagerAnnounceDir:
    """Rank 6: the per-run announce dir is born at run START, not on the first
    terminal marker — so the client's one-readdir census owns the lifecycle from
    the tick a task begins executing, never falling back to the 20-25 min
    status-reporter walk during the first task's compute window."""

    def test_announce_dir_created_before_terminal_marker(self, tmp_path, monkeypatch):
        # Use the self-recursion guard's early exit (exit 3): it aborts BEFORE
        # spawning anything and BEFORE any _write_announcement, so the ONLY thing
        # that could have created the announce dir is the eager pre-create. No
        # executor spawn → platform-independent (no _posix_shell_executor mark).
        hpc = _scaffold(
            tmp_path,
            executor="python3 .hpc/_hpc_dispatch.py",  # re-invokes dispatcher → exit 3
            result_dir_template=str(tmp_path / "results" / "{task_id}"),
            kwargs_per_task=[{}],
            run_id="eager_run",
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "eager_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        # Tripwire: prove NO terminal marker was written on this early-exit path.
        marker_writes: list[int] = []
        monkeypatch.setattr(
            dispatch, "_write_announcement", lambda *a, **k: marker_writes.append(1)
        )

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        # Aborted at the self-recursion guard — before spawning, before any marker.
        assert exc_info.value.code == dispatch._EXIT_NO_RUNNER
        assert marker_writes == []
        # Yet the census-owning announce dir already EXISTS — created eagerly.
        assert (hpc / "announce" / "eager_run").is_dir()


@_posix_shell_executor
class TestDispatchStaleWipRetry:
    def test_stale_wip_renamed_on_retry(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        result_root.mkdir()
        task_dir = result_root / "1"
        task_dir.mkdir()
        stale_wip = task_dir / "_wip_1"
        stale_wip.mkdir()
        (stale_wip / "partial.csv").write_text("stale partial\n")

        hpc = _scaffold(
            tmp_path,
            executor='echo fresh > "$RESULT_DIR/results_task_1.csv"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}],
        )

        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0

        renamed = [p for p in task_dir.iterdir() if re.match(r"^_wip_1_failed_\d+$", p.name)]
        assert len(renamed) == 1
        assert (renamed[0] / "partial.csv").read_text().strip() == "stale partial"
        assert not stale_wip.exists()
        assert (task_dir / "results_task_1.csv").read_text().strip() == "fresh"


class TestDispatchSidecarSchemaVersion:
    def test_missing_schema_version_exits_2(self, tmp_path, monkeypatch):
        hpc = _scaffold(
            tmp_path,
            executor="true",
            result_dir_template=str(tmp_path / "r"),
            kwargs_per_task=[{}],
        )
        # Deliberately mangle the sidecar.
        sidecar_path = hpc / "runs" / "test_run.json"
        data = json.loads(sidecar_path.read_text())
        data.pop("sidecar_schema_version")
        sidecar_path.write_text(json.dumps(data))

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 2

    def test_wrong_schema_version_exits_2(self, tmp_path, monkeypatch):
        hpc = _scaffold(
            tmp_path,
            executor="true",
            result_dir_template=str(tmp_path / "r"),
            kwargs_per_task=[{}],
        )
        sidecar_path = hpc / "runs" / "test_run.json"
        data = json.loads(sidecar_path.read_text())
        data["sidecar_schema_version"] = 999
        sidecar_path.write_text(json.dumps(data))

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 2


class TestCheckResultsIgnoresWip:
    def test_check_results_ignores_wip(self, tmp_path):
        result_dir = tmp_path / "results"
        result_dir.mkdir()
        valid_csv = result_dir / "results_task_1.csv"
        valid_csv.write_text("col_a,col_b\n1,2\n")
        wip_dir = result_dir / "_wip_0"
        wip_dir.mkdir()
        (wip_dir / "results_task_2.csv").write_text("col_a,col_b\n3,4\n")

        results = check_results(result_dir, total_tasks=2)
        # Flat-dir scan assigns 0-based positions; one valid CSV → key 0.
        assert 0 in results
        assert 1 not in results
        assert len(results) == 1


@_posix_shell_executor
class TestKwargNamespaceOnly:
    def test_default_exports_both_forms(self, tmp_path, monkeypatch):
        """Without HPC_KW_NAMESPACE_ONLY, executor sees both HPC_KW_X and X."""
        result_root = tmp_path / "results"
        # Executor stamps both env-var forms into separate files so we
        # can check both are present in the dispatcher's env without
        # depending on shell-export semantics.
        hpc = _scaffold(
            tmp_path,
            executor=(
                'echo "$HPC_KW_HORIZON" > "$RESULT_DIR/kw_form.txt" && '
                'echo "$HORIZON" > "$RESULT_DIR/bare_form.txt"'
            ),
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{"horizon": 5}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        out_dir = result_root / "0"
        assert (out_dir / "kw_form.txt").read_text().strip() == "5"
        assert (out_dir / "bare_form.txt").read_text().strip() == "5"

    def test_namespace_only_skips_bare_form(self, tmp_path, monkeypatch):
        """With HPC_KW_NAMESPACE_ONLY=1, bare-uppercase HORIZON is NOT exported."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor=(
                'echo "$HPC_KW_HORIZON" > "$RESULT_DIR/kw_form.txt" && '
                'echo "${HORIZON:-UNSET}" > "$RESULT_DIR/bare_form.txt"'
            ),
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{"horizon": 5}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_KW_NAMESPACE_ONLY", "1")
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        out_dir = result_root / "0"
        assert (out_dir / "kw_form.txt").read_text().strip() == "5"
        assert (out_dir / "bare_form.txt").read_text().strip() == "UNSET"


class TestIdempotencyBypass:
    def _seed_metrics(self, result_dir, content="{}"):
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "metrics.json").write_text(content)

    def test_metrics_present_skips_by_default(self, tmp_path, monkeypatch):
        """Existing metrics.json triggers the idempotency exit-0 skip."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo NEVER_RUN > "$RESULT_DIR/marker.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        self._seed_metrics(result_root / "0", '{"value": 1}')
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        # Executor must NOT have run.
        assert not (result_root / "0" / "marker.txt").exists()

    @_posix_shell_executor
    def test_force_rerun_bypasses_skip(self, tmp_path, monkeypatch):
        """HPC_FORCE_RERUN=1 runs the executor even with metrics.json present."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo RAN > "$RESULT_DIR/marker.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        self._seed_metrics(result_root / "0", '{"value": 1}')
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_FORCE_RERUN", "1")
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert (result_root / "0" / "marker.txt").read_text().strip() == "RAN"

    @_posix_shell_executor
    def test_cmd_sha_mismatch_bypasses_skip(self, tmp_path, monkeypatch):
        """A stamped .hpc_cmd_sha that disagrees with the sidecar forces re-run."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo RAN > "$RESULT_DIR/marker.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        # Sidecar default cmd_sha is "deadbeef"*8; stamp a different one.
        result_dir = result_root / "0"
        self._seed_metrics(result_dir, '{"value": 1}')
        (result_dir / ".hpc_cmd_sha").write_text("0" * 64)

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert (result_dir / "marker.txt").read_text().strip() == "RAN"
        # After a successful re-run, the marker should now match the sidecar.
        assert (result_dir / ".hpc_cmd_sha").read_text() == "deadbeef" * 8

    def test_cmd_sha_match_preserves_skip(self, tmp_path, monkeypatch):
        """A stamped .hpc_cmd_sha that matches the sidecar still skips."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo NEVER_RUN > "$RESULT_DIR/marker.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        result_dir = result_root / "0"
        self._seed_metrics(result_dir, '{"value": 1}')
        (result_dir / ".hpc_cmd_sha").write_text("deadbeef" * 8)

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert not (result_dir / "marker.txt").exists()

    @_posix_shell_executor
    def test_successful_run_stamps_cmd_sha(self, tmp_path, monkeypatch):
        """A fresh successful run writes .hpc_cmd_sha next to the result files."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo done > "$RESULT_DIR/out.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        marker = result_root / "0" / ".hpc_cmd_sha"
        assert marker.is_file()
        assert marker.read_text() == "deadbeef" * 8


def test_exit_no_runner_constant_in_lockstep_with_preamble() -> None:
    """The dispatcher's no-runner exit code is consumed by the template's
    retry helper as a TERMINAL code (``HPC_DISPATCH_EXIT_NO_RUNNER`` in
    hpc_preamble.sh). Pin the value so a change here without updating the
    shell side gets caught."""
    assert dispatch._EXIT_NO_RUNNER == 3


def test_exit_no_output_constant_in_lockstep_with_preamble() -> None:
    """#16 (proving run #5): the "exit 0 but produced no output" code is a
    TERMINAL code the retry helper must NOT retry (``HPC_DISPATCH_EXIT_NO_OUTPUT``
    in hpc_preamble.sh). Pin the value AND cross-check the shell asset so a
    change on one side without the other is caught."""
    from hpc_agent import _PACKAGE_ROOT

    assert dispatch._EXIT_NO_OUTPUT == 4
    preamble = (
        _PACKAGE_ROOT
        / "execution"
        / "mapreduce"
        / "templates"
        / "runtime"
        / "common"
        / "hpc_preamble.sh"
    ).read_text(encoding="utf-8")
    assert "HPC_DISPATCH_EXIT_NO_OUTPUT=4" in preamble
    # And the retry loop treats it as terminal (breaks, does not back off).
    assert 'rc" -eq "$HPC_DISPATCH_EXIT_NO_OUTPUT' in preamble


class TestDispatchEmptyOutputIsFailure:
    """#16 (proving run #5, the FALSE GREEN): an executor that exits 0 but
    writes NOTHING to $RESULT_DIR produced no result. Promoting the empty WIP as
    a success let every task read ``complete``, the 1-task canary pass, and only
    the harvest discover there was nothing to aggregate. The dispatcher must
    convert exit-0-with-empty-WIP into a task FAILURE so the reporter counts it
    failed and the canary catches it on one task."""

    @_posix_shell_executor
    def test_empty_output_exit0_becomes_failure(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="true",  # exits 0, writes nothing to $RESULT_DIR
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        # Non-zero, distinct, terminal exit — NOT a promoted success.
        assert exc_info.value.code == dispatch._EXIT_NO_OUTPUT
        result_dir = result_root / "0"
        # WIP preserved with a failure-capture log (like a real failure), so the
        # empty-output cause is diagnosable and NOT silently promoted.
        log = result_dir / "_wip_0" / "_hpc_dispatch_error.log"
        assert log.is_file()
        assert f"exit_code={dispatch._EXIT_NO_OUTPUT}" in log.read_text()
        # No success marker stamped — a re-run must not be idempotency-skipped.
        assert not (result_dir / ".hpc_cmd_sha").exists()
        # The recorded per-task exit code (what the canary reads) is non-zero.
        runtime = json.loads((result_dir / "_runtime.json").read_text())
        assert runtime["exit_code"] == dispatch._EXIT_NO_OUTPUT

    @_posix_shell_executor
    def test_trace_only_output_is_failure(self, tmp_path, monkeypatch):
        """#28: an executor that emits ONLY the framework data-trace transport
        file (``_trace.jsonl``) but no real result must be treated as
        empty-output — the trace is telemetry, not a produced result, so
        promoting it would be the same FALSE GREEN #16 guards against."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo \'{"trace": 1}\' > "$RESULT_DIR/_trace.jsonl"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == dispatch._EXIT_NO_OUTPUT
        result_dir = result_root / "0"
        # Not promoted: no trace file lands at the top level, no success marker.
        assert not (result_dir / "_trace.jsonl").exists()
        assert not (result_dir / ".hpc_cmd_sha").exists()
        runtime = json.loads((result_dir / "_runtime.json").read_text())
        assert runtime["exit_code"] == dispatch._EXIT_NO_OUTPUT

    @_posix_shell_executor
    def test_trace_beside_real_output_still_completes(self, tmp_path, monkeypatch):
        """When a real result exists, the trace is promoted alongside it — the
        guard only rejects a trace-ONLY (output-less) dir."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor=(
                'echo \'{"value": 1, "n_samples": 1}\' > "$RESULT_DIR/metrics.json"; '
                'echo \'{"trace": 1}\' > "$RESULT_DIR/_trace.jsonl"'
            ),
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0
        result_dir = result_root / "0"
        assert (result_dir / "metrics.json").is_file()
        assert (result_dir / "_trace.jsonl").is_file()  # promoted alongside output
        assert not (result_dir / "_wip_0").exists()

    @_posix_shell_executor
    def test_real_metrics_json_still_completes(self, tmp_path, monkeypatch):
        """A task that writes metrics.json to $RESULT_DIR is promoted, exit 0
        (the empty-output guard must not touch a real result)."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo \'{"value": 1, "n_samples": 1}\' > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0
        result_dir = result_root / "0"
        assert (result_dir / "metrics.json").is_file()
        assert not (result_dir / "_wip_0").exists()  # promoted, WIP cleaned up
        assert (result_dir / ".hpc_cmd_sha").is_file()  # success stamps the marker


class TestDispatchFailsLoudOnSelfRecursion:
    """#162: when the run sidecar's per-task ``executor`` would re-invoke the
    dispatcher itself (submit-flow synthesized it from the job-script command
    instead of a real per-task command), the dispatcher must ABORT with a
    clear error + exit 3 BEFORE spawning anything — never self-recurse."""

    @pytest.mark.parametrize(
        "bad_executor",
        [
            "python3 .hpc/_hpc_dispatch.py",
            "python3 /abs/path/.hpc/_hpc_dispatch.py --flag x",
            "python3 dispatch.py",
        ],
    )
    def test_self_referential_executor_exits_3(self, tmp_path, monkeypatch, bad_executor):
        result_root = tmp_path / "results"
        sentinel = tmp_path / "should_never_run.flag"
        # The executor string itself would create a sentinel if it were
        # ever shell-run; the abort must happen before any spawn.
        hpc = _scaffold(
            tmp_path,
            executor=bad_executor,
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        # Distinct terminal exit code (not the generic 1 / schema 2).
        assert exc_info.value.code == 3
        assert not sentinel.exists()
        # No WIP dir should have been created — we aborted before exec.
        assert not (result_root / "0" / "_wip_0").exists()

    def test_clear_error_message_on_stderr(self, tmp_path, monkeypatch, capsys):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="python3 .hpc/_hpc_dispatch.py",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit):
            dispatch.main()
        err = capsys.readouterr().err
        assert "re-invokes the dispatcher" in err
        assert "per-task" in err

    @_posix_shell_executor
    def test_valid_cli_py_executor_is_not_flagged(self, tmp_path, monkeypatch):
        """A legitimate ``.hpc/cli.py`` per-task command must NOT trip the
        self-recursion guard — only the dispatcher's own filename does."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo ok > "$RESULT_DIR/out.txt"',  # stands in for cli.py
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0


class TestDispatchFailureCapture:
    """#161: a failed dispatch must record WHY into the failure dir
    instead of leaving an EMPTY ``_wip_*_failed_*``."""

    def test_stderr_captured_into_wip_on_failure(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            # Emit a recognizable line to stderr, then fail.
            executor='echo "BOOM: kaboom traceback line" >&2 && exit 7',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 7

        log = result_root / "0" / "_wip_0" / "_hpc_dispatch_error.log"
        assert log.is_file(), "failure capture log not written"
        text = log.read_text()
        assert "exit_code=7" in text
        assert "BOOM: kaboom traceback line" in text
        # The captured command must be recorded for diagnosis.
        assert "exit 7" in text

    @_posix_shell_executor
    def test_no_capture_log_on_success(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo "noise" >&2 && echo done > "$RESULT_DIR/out.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        # On success the WIP dir is removed entirely; no capture log lingers.
        assert not (result_root / "0" / "_wip_0").exists()


# ─── #195 part C: dispatcher warns on unset $HPC_KW_* references ─────────────


def test_warn_unset_kwarg_refs_flags_missing(capsys: pytest.CaptureFixture[str]) -> None:
    """A command referencing $HPC_KW_SAMPLES with no such kwarg exported is
    flagged (it would expand to empty and fail the task — #195)."""
    executor = "python3 mc.py --seed $HPC_KW_SEED --samples $HPC_KW_SAMPLES"
    env = {"HPC_KW_SEED": "0"}  # SAMPLES never exported
    missing = dispatch._warn_unset_kwarg_refs(executor, env)
    assert missing == ["HPC_KW_SAMPLES"]
    err = capsys.readouterr().err
    assert "HPC_KW_SAMPLES" in err
    assert "samples" in err  # names the kwarg + the #195 remediation
    assert "#195" in err


def test_warn_unset_kwarg_refs_brace_form(capsys: pytest.CaptureFixture[str]) -> None:
    """The ``${HPC_KW_X}`` brace form is matched too."""
    missing = dispatch._warn_unset_kwarg_refs("run --x ${HPC_KW_FOO}", {})
    assert missing == ["HPC_KW_FOO"]


def test_warn_unset_kwarg_refs_silent_when_all_present() -> None:
    """Every referenced HPC_KW_* is exported → no findings, no warning."""
    executor = "python3 mc.py --seed $HPC_KW_SEED --samples $HPC_KW_SAMPLES"
    env = {"HPC_KW_SEED": "0", "HPC_KW_SAMPLES": "10000"}
    assert dispatch._warn_unset_kwarg_refs(executor, env) == []


def test_warn_unset_kwarg_refs_ignores_bare_env_vars() -> None:
    """Only the framework-owned HPC_KW_ namespace is checked — a bare $HOME or
    $SAMPLES reference is left alone (can't tell it from a real env var)."""
    assert dispatch._warn_unset_kwarg_refs("run --home $HOME --s $SAMPLES", {}) == []


def test_latest_checkpoint_picks_highest_nonempty(tmp_path: Path) -> None:
    # #294 PR3: the dispatcher's stdlib resume-point finder picks the
    # highest-iteration NON-empty checkpoint, skips 0-byte files, and returns
    # "" when there's nothing to resume from.
    ckdir = tmp_path / "_checkpoints"
    ckdir.mkdir()
    (ckdir / "checkpoint-1.pkl").write_bytes(b"a")
    (ckdir / "checkpoint-5.pkl").write_bytes(b"b")
    (ckdir / "checkpoint-3.pkl").write_bytes(b"c")
    (ckdir / "checkpoint-9.pkl").write_bytes(b"")  # 0-byte → ignored
    (ckdir / "not-a-checkpoint.txt").write_text("x")
    assert dispatch._latest_checkpoint(str(ckdir)) == str(ckdir / "checkpoint-5.pkl")
    # Missing dir → no resume point, no raise.
    assert dispatch._latest_checkpoint(str(tmp_path / "absent")) == ""


def test_latest_checkpoint_sees_petsc_binary_steps(tmp_path: Path) -> None:
    """#294 + solver adapters: the stdlib resume-point finder is widened to
    step-indexed PETSc binary dumps, so a resumed petsc4py executor also gets
    HPC_RESUME_FROM. Wrapper-path dumps (petsc-solution.bin) stay invisible —
    the instrumented wrapper rotates/consumes those itself."""
    ckdir = tmp_path / "_checkpoints"
    ckdir.mkdir()
    (ckdir / "checkpoint-2.pkl").write_bytes(b"a")
    (ckdir / "checkpoint-6.petscbin").write_bytes(b"b")
    (ckdir / "petsc-solution.bin").write_bytes(b"c")
    assert dispatch._latest_checkpoint(str(ckdir)) == str(ckdir / "checkpoint-6.petscbin")
    # Equal iteration across formats: pickle wins, deterministically
    # (pre-petsc behavior preserved for runs that only ever write pickles).
    (ckdir / "checkpoint-6.pkl").write_bytes(b"d")
    assert dispatch._latest_checkpoint(str(ckdir)) == str(ckdir / "checkpoint-6.pkl")


# ---------------------------------------------------------------------------
# BUG 5 — frozen-manifest fast path (trial_params in sidecar)
# ---------------------------------------------------------------------------


def _scaffold_with_manifest(
    tmp_path: Path,
    *,
    executor: str,
    trial_params: list[dict],
    run_id: str = "test_run",
) -> Path:
    """Scaffold a .hpc/ with a sidecar that carries trial_params (the frozen manifest).

    Does NOT write tasks.py — the fast path must not need it.
    Returns the .hpc/ path.
    """
    result_root = tmp_path / "results"
    hpc = tmp_path / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    make_sidecar_json(
        tmp_path,
        run_id=run_id,
        executor=executor,
        result_dir_template=str(result_root / "{task_id}"),
        task_count=len(trial_params),
        tasks_py_sha="abc123",
        trial_params=trial_params,
    )
    return hpc


class TestFrozenManifest:
    """dispatcher reads kwargs from sidecar trial_params, not by re-importing tasks.py."""

    @_posix_shell_executor
    def test_fast_path_skips_tasks_py(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When trial_params is present in the sidecar the dispatcher succeeds
        even if tasks.py does not exist — tasks.py is never imported."""
        hpc = _scaffold_with_manifest(
            tmp_path,
            # Writes a result so the run is a genuine success (an empty-output
            # exit-0 is now a task failure — proving-run-5 finding 16).
            executor='echo ok > "$RESULT_DIR/out.txt"',
            trial_params=[{"seed": 0}, {"seed": 1}],
        )
        # Deliberately do NOT write tasks.py — the fast path must not need it.
        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0

    @_posix_shell_executor
    def test_fast_path_exports_kwargs_as_hpc_kw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Kwargs from trial_params are exported as HPC_KW_* in the child env.

        We verify this indirectly: if the dispatcher exits 0 for task_id=0
        and does NOT raise about a missing tasks.py, the frozen params were
        used. The actual env-export is exercised by the POSIX executor tests
        (TestDispatchAtomicOutput), but the kwargs-to-env wiring is shared
        code so checking the sidecar fast path ends at a valid kwargs dict
        is sufficient here to confirm the dispatch path is taken.
        """
        params = [{"lr": "0.01", "batch": "32"}, {"lr": "0.001", "batch": "64"}]
        hpc = _scaffold_with_manifest(
            tmp_path,
            executor='echo ok > "$RESULT_DIR/out.txt"',
            trial_params=params,
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0

    @_posix_shell_executor
    def test_fallback_to_tasks_py_when_no_trial_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old sidecars without trial_params still load tasks.py (backward compat)."""
        hpc = tmp_path / ".hpc"
        write_hpc_tasks(hpc, [{"seed": 7}])
        make_sidecar_json(
            tmp_path,
            run_id="old_run",
            executor='echo ok > "$RESULT_DIR/out.txt"',
            result_dir_template=str(tmp_path / "results" / "{task_id}"),
            task_count=1,
            tasks_py_sha="abc123",
            # No trial_params key — simulates a pre-BUG5-fix sidecar.
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "old_run")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0

    def test_fast_path_task_id_out_of_range_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """task_id beyond the manifest length → exit 1 with a clear message."""
        hpc = _scaffold_with_manifest(
            tmp_path,
            executor="true",
            trial_params=[{"seed": 0}],  # only 1 task
        )
        monkeypatch.setenv("HPC_TASK_ID", "5")  # out of range
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 1
        assert "out of range" in capsys.readouterr().err

    def test_fast_path_entry_not_dict_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If trial_params[task_id] is not a dict the dispatcher exits 1."""
        hpc = tmp_path / ".hpc"
        hpc.mkdir(parents=True, exist_ok=True)
        make_sidecar_json(
            tmp_path,
            run_id="test_run",
            executor="true",
            result_dir_template=str(tmp_path / "results" / "{task_id}"),
            task_count=1,
            tasks_py_sha="abc123",
            trial_params=["not-a-dict"],  # malformed
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 1
        assert "dict" in capsys.readouterr().err

    def test_fast_path_task_count_mismatch_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """task_count in sidecar > len(trial_params) → IndexError path → exit 1."""
        hpc = tmp_path / ".hpc"
        hpc.mkdir(parents=True, exist_ok=True)
        make_sidecar_json(
            tmp_path,
            run_id="test_run",
            executor="true",
            result_dir_template=str(tmp_path / "results" / "{task_id}"),
            task_count=10,  # claims 10 tasks
            tasks_py_sha="abc123",
            trial_params=[{"seed": 0}],  # but only 1 entry
        )
        monkeypatch.setenv("HPC_TASK_ID", "5")  # valid by task_count, out of trial_params
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "trial_params" in err and "re-submit" in err


class TestServiceEnvPassthrough:
    """HPC_SERVICE_ENV → HPC_SERVICE_* task-env passthrough (#231 Tier 1).

    The dispatcher used to reach for ``hpc_agent.ops.recover.service`` at
    dispatch time, but deploy ships no ``hpc_agent`` package — every array
    task of a service_env run died with ModuleNotFoundError before the
    executor spawned. The logic is now inlined stdlib-only; these tests pin
    both the inlined behavior and its parity with the control-plane copy.
    """

    def test_inlined_injection_matches_control_plane_copy(self):
        """Sync pin: the stdlib twin must behave exactly like
        ``ops.recover.service.inject_service_env`` (the deliberate
        cluster-side duplication, same discipline as _CHECKPOINT_RES)."""
        from hpc_agent.ops.recover import service

        assert dispatch._SERVICE_ENV_NAMESPACE == service.SERVICE_ENV_NAMESPACE
        cases = [
            {"compile_url": "http://n01:8080", "Port": 8081, "token": None},
            {},
            None,
        ]
        for service_env in cases:
            env_a: dict = {"KEEP": "me"}
            env_b: dict = {"KEEP": "me"}
            ret_a = dispatch._inject_service_env(env_a, service_env)
            ret_b = service.inject_service_env(env_b, service_env)
            assert env_a == env_b, f"divergence for {service_env!r}"
            # Both return the mutated env for caller convenience.
            assert ret_a is env_a
            assert ret_b is env_b

    def test_inlined_injection_namespaces_and_stringifies(self):
        env: dict = {}
        dispatch._inject_service_env(env, {"compile_url": "http://n01:8080", "port": 8081})
        assert env == {
            "HPC_SERVICE_COMPILE_URL": "http://n01:8080",
            "HPC_SERVICE_PORT": "8081",
        }

    @_posix_shell_executor
    def test_service_env_reaches_executor(self, tmp_path, monkeypatch):
        """End-to-end: a JSON HPC_SERVICE_ENV lands as $HPC_SERVICE_* in the
        executor's env — with no hpc_agent package importable cluster-side."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo "$HPC_SERVICE_COMPILE_URL" > "$RESULT_DIR/service.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_SERVICE_ENV", json.dumps({"compile_url": "http://n01:8080"}))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert (result_root / "0" / "service.txt").read_text().strip() == "http://n01:8080"

    @_posix_shell_executor
    def test_malformed_service_env_warns_and_still_runs(self, tmp_path, monkeypatch, capsys):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo ok > "$RESULT_DIR/out.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_SERVICE_ENV", "{not json")
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert "malformed HPC_SERVICE_ENV" in capsys.readouterr().err


class TestStaleMetricsQuarantine:
    """The cmd_sha-changed re-run path must quarantine the PREVIOUS
    experiment's metrics.json before re-running: left in place, a failed (or
    still-running) new attempt lets status/combiner count the stale file as
    this run's completed result."""

    def _seed_stale(self, result_dir, content='{"value": 1}'):
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "metrics.json").write_text(content)
        # Sidecar default cmd_sha is "deadbeef"*8; stamp a different one so
        # the cmd_sha-changed re-run path fires.
        (result_dir / ".hpc_cmd_sha").write_text("0" * 64)

    @_posix_shell_executor
    def test_failed_rerun_leaves_no_stale_completion_marker(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="exit 1",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        result_dir = result_root / "0"
        self._seed_stale(result_dir)

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 1

        # The stale marker is GONE from the completion-scan namespace…
        assert not (result_dir / "metrics.json").exists()
        # …and preserved as evidence under the _wip_ family every scanner skips.
        quarantined = list(result_dir.glob("_wip_0_stale_metrics_*.json"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text() == '{"value": 1}'
        # The reporter must NOT count this task complete anymore (the
        # quarantined name lives in the _wip_ family every scanner skips).
        assert check_results(result_dir, total_tasks=1, file_glob="*") == {}

    @_posix_shell_executor
    def test_successful_rerun_promotes_fresh_metrics(self, tmp_path, monkeypatch):
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo \'{"value": 2}\' > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        result_dir = result_root / "0"
        self._seed_stale(result_dir)

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        # Fresh result promoted; stale evidence retained alongside.
        assert json.loads((result_dir / "metrics.json").read_text()) == {"value": 2}
        assert len(list(result_dir.glob("_wip_0_stale_metrics_*.json"))) == 1
        assert (result_dir / ".hpc_cmd_sha").read_text() == "deadbeef" * 8

    @_posix_shell_executor
    def test_force_rerun_does_not_quarantine(self, tmp_path, monkeypatch):
        """HPC_FORCE_RERUN re-runs the SAME experiment (matching cmd_sha) —
        the prior result stays a valid last-known completion, not stale."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor="exit 1",
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}],
        )
        result_dir = result_root / "0"
        result_dir.mkdir(parents=True)
        (result_dir / "metrics.json").write_text('{"value": 1}')
        (result_dir / ".hpc_cmd_sha").write_text("deadbeef" * 8)  # matches sidecar

        monkeypatch.setenv("HPC_TASK_ID", "0")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_FORCE_RERUN", "1")
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 1
        assert (result_dir / "metrics.json").read_text() == '{"value": 1}'
        assert list(result_dir.glob("_wip_0_stale_metrics_*.json")) == []


class TestPartialReproductionInclude:
    """HPC_TASK_INCLUDE — the partial-reproduction execution restriction (T6).

    A partial reproduction keeps the FULL task array (same trial_params /
    cmd_sha) and threads the selected task indices through the job env as an
    include-list. A non-selected index exits 0 IMMEDIATELY — before resolving
    kwargs, formatting result_dir, or spawning — so its slot costs milliseconds
    and it writes NO output (the reduce never sees a spurious row).
    """

    def test_included_helper_parsing(self) -> None:
        """The include predicate: absent/blank/malformed → no restriction; else membership."""
        assert dispatch._task_is_included(0, {}) is True
        assert dispatch._task_is_included(0, {"HPC_TASK_INCLUDE": ""}) is True
        assert dispatch._task_is_included(0, {"HPC_TASK_INCLUDE": "   "}) is True
        assert dispatch._task_is_included(1, {"HPC_TASK_INCLUDE": "0,2,4"}) is False
        assert dispatch._task_is_included(4, {"HPC_TASK_INCLUDE": "0,2,4"}) is True
        assert dispatch._task_is_included(5, {"HPC_TASK_INCLUDE": " 5 , 7 "}) is True
        # Non-integer garbage parses to an empty set → treated as no restriction
        # (never silently skip the whole array on a bad env var).
        assert dispatch._task_is_included(3, {"HPC_TASK_INCLUDE": "nope"}) is True

    def test_excluded_index_exits_0_without_running(self, tmp_path, monkeypatch):
        """A task_id absent from HPC_TASK_INCLUDE exits 0 fast — no result dir, no output."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo NEVER_RUN > "$RESULT_DIR/marker.txt"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}, {}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_TASK_INCLUDE", "0,2")  # task 1 excluded
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        # Exited before result_dir was even created → no dir, no marker.
        assert not (result_root / "1").exists()

    @_posix_shell_executor
    def test_included_index_runs(self, tmp_path, monkeypatch):
        """A task_id present in HPC_TASK_INCLUDE runs the executor normally."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo RAN > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}, {}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "2")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setenv("HPC_TASK_INCLUDE", "0,2")  # task 2 included
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert (result_root / "2" / "metrics.json").read_text().strip() == "RAN"

    @_posix_shell_executor
    def test_no_include_runs_full(self, tmp_path, monkeypatch):
        """Absent HPC_TASK_INCLUDE runs every task (an ordinary full run)."""
        result_root = tmp_path / "results"
        hpc = _scaffold(
            tmp_path,
            executor='echo RAN > "$RESULT_DIR/metrics.json"',
            result_dir_template=str(result_root / "{task_id}"),
            kwargs_per_task=[{}, {}],
        )
        monkeypatch.setenv("HPC_TASK_ID", "1")
        monkeypatch.setenv("HPC_RUN_ID", "test_run")
        monkeypatch.setenv("HPC_TASKS_PATH", str(hpc / "tasks.py"))
        monkeypatch.setattr(dispatch, "__file__", str(hpc / "_hpc_dispatch.py"), raising=False)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()
        assert exc_info.value.code == 0
        assert (result_root / "1" / "metrics.json").read_text().strip() == "RAN"
