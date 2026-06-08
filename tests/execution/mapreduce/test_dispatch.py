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
