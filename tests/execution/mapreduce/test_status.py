"""Tests for hpc_agent.execution.mapreduce.reduce.status — check_results and report_status."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

from hpc_agent.execution.mapreduce.reduce.status import (
    check_results,
    check_results_from_tasks,
    report_status,
)


class TestCheckResultsIgnoresWip:
    def test_check_results_ignores_wip(self, tmp_path):
        result_dir = tmp_path / "results"
        result_dir.mkdir()

        # Write a valid CSV in the result dir (header + 1 data row)
        valid_csv = result_dir / "results_task_1.csv"
        valid_csv.write_text("col_a,col_b\n1,2\n")

        # Create a _wip_0 subdir with another CSV that should be ignored
        wip_dir = result_dir / "_wip_0"
        wip_dir.mkdir()
        wip_csv = wip_dir / "results_task_2.csv"
        wip_csv.write_text("col_a,col_b\n3,4\n")

        results = check_results(result_dir, total_tasks=2)

        # Only one task should be found (flat scan, 0-based position); the
        # _wip_ CSV is skipped.
        assert 0 in results
        assert 1 not in results
        assert len(results) == 1


class TestReportStatus:
    def test_report_status_basic(self, tmp_path):
        # Patch scheduler query functions to avoid real subprocess calls.
        with (
            patch(
                "hpc_agent.execution.mapreduce.reduce.status.detect_scheduler", return_value="slurm"
            ),
            patch("hpc_agent.infra.backends.query.query_sacct", return_value={}),
        ):
            result = report_status(
                result_dir=tmp_path,
                job_ids=["12345"],
                total_tasks=1,
                scheduler="slurm",
            )

        assert "total_tasks" in result
        assert result["total_tasks"] == 1
        assert "summary" in result
        # resource_usage is additive & always present with the canonical shape.
        assert "resource_usage" in result
        ru = result["resource_usage"]
        for k in ("cpu_hours", "gpu_hours", "elapsed_hours", "tasks_counted"):
            assert k in ru


class TestPreemptedDetection:
    """The reporter surfaces a fresh, per-poll scheduler-side preempt signal
    (exit 130/143 or state PREEMPTED) so the monitor's auto-resume gate reads
    it straight off last_status (#299)."""

    def test_is_preempted_task_exit_codes_and_state(self):
        from hpc_agent.execution.mapreduce.reduce.status import _is_preempted_task

        assert _is_preempted_task({"state": "FAILED", "exit_code": "130:0"}) is True
        assert _is_preempted_task({"state": "FAILED", "exit_code": "143"}) is True
        assert _is_preempted_task({"state": "PREEMPTED", "exit_code": "0:0"}) is True
        # OOM / real failure / running are NOT preempted.
        assert _is_preempted_task({"state": "OUT_OF_MEMORY", "exit_code": "137:0"}) is False
        assert _is_preempted_task({"state": "FAILED", "exit_code": "1:0"}) is False
        assert _is_preempted_task({"state": "RUNNING", "exit_code": None}) is False
        assert _is_preempted_task({}) is False

    def test_preempted_ids_from_tasks_sorts_and_filters(self):
        from hpc_agent.execution.mapreduce.reduce.status import _preempted_ids_from_tasks

        tasks = {
            "3": {"state": "FAILED", "exit_code": "130:0"},
            "1": {"state": "PREEMPTED"},
            "2": {"state": "OUT_OF_MEMORY", "exit_code": "137:0"},  # OOM excluded
        }
        assert _preempted_ids_from_tasks(tasks) == [1, 3]

    def test_report_status_folds_preempted_task_ids(self, tmp_path):
        # query output is now 0-based HpcTaskId.
        fake_query = {
            "tasks": {
                0: {"state": "FAILED", "exit_code": "130:0"},  # preempted
                1: {"state": "OUT_OF_MEMORY", "exit_code": "137:0"},  # OOM
            },
            "errors": [],
        }
        with (
            patch(
                "hpc_agent.execution.mapreduce.reduce.status.detect_scheduler", return_value="slurm"
            ),
            patch("hpc_agent.infra.backends.query.query_sacct", return_value=fake_query),
        ):
            result = report_status(
                result_dir=tmp_path, job_ids=["12345"], total_tasks=2, scheduler="slurm"
            )
        assert result["preempted_task_ids"] == [0]

    def test_report_status_omits_key_when_none_preempted(self, tmp_path):
        fake_query = {"tasks": {0: {"state": "FAILED", "exit_code": "1:0"}}, "errors": []}
        with (
            patch(
                "hpc_agent.execution.mapreduce.reduce.status.detect_scheduler", return_value="slurm"
            ),
            patch("hpc_agent.infra.backends.query.query_sacct", return_value=fake_query),
        ):
            result = report_status(
                result_dir=tmp_path, job_ids=["12345"], total_tasks=1, scheduler="slurm"
            )
        assert "preempted_task_ids" not in result


class TestReportStatusResourceUsage:
    def test_resource_usage_sums_from_query(self, tmp_path):
        # Fake query returns two running tasks with usage data so we can
        # verify the report-level rollup wires through correctly.
        # query output is now 0-based HpcTaskId.
        fake_query = {
            "tasks": {
                0: {"state": "RUNNING", "elapsed_s": 3600, "cpu_s": 4 * 3600, "gpu_s": 3600},
                1: {"state": "RUNNING", "elapsed_s": 1800, "cpu_s": 4 * 1800, "gpu_s": 0},
            },
            "errors": [],
        }
        with (
            patch(
                "hpc_agent.execution.mapreduce.reduce.status.detect_scheduler", return_value="slurm"
            ),
            patch("hpc_agent.infra.backends.query.query_sacct", return_value=fake_query),
        ):
            result = report_status(
                result_dir=tmp_path,
                job_ids=["12345"],
                total_tasks=2,
                scheduler="slurm",
            )
        ru = result["resource_usage"]
        assert ru["cpu_hours"] == round((4 * 3600 + 4 * 1800) / 3600.0, 4)
        assert ru["gpu_hours"] == 1.0
        assert ru["tasks_counted"] == 2


class TestHeaderOnlyCsv:
    """Header-only CSVs should count as complete by default (P1.4 bug fix).

    A legitimately-empty result (e.g. a zero-result task) used
    to be marked failed and trigger infinite auto-resubmit in ``/status``.
    The default is now non-zero byte = complete; callers opt into the stricter
    check with ``min_rows>0``.
    """

    def test_header_only_csv_complete_by_default(self, tmp_path):
        result_dir = tmp_path / "results"
        # task_N subdir is 0-based (renders the dispatcher's HPC_TASK_ID).
        task_dir = result_dir / "task_0"
        task_dir.mkdir(parents=True)
        (task_dir / "out.csv").write_text("col_a,col_b\n")  # header only

        results = check_results(result_dir, total_tasks=1)

        assert 0 in results
        assert results[0]["status"] == "complete"

    def test_header_only_csv_incomplete_with_min_rows(self, tmp_path):
        result_dir = tmp_path / "results"
        task_dir = result_dir / "task_0"
        task_dir.mkdir(parents=True)
        (task_dir / "out.csv").write_text("col_a,col_b\n")  # header only

        results = check_results(result_dir, total_tasks=1, min_rows=1)

        assert 0 not in results

    def test_zero_byte_file_still_incomplete(self, tmp_path):
        """A truly empty (zero-byte) file is still treated as incomplete."""
        result_dir = tmp_path / "results"
        task_dir = result_dir / "task_0"
        task_dir.mkdir(parents=True)
        (task_dir / "out.csv").write_text("")

        results = check_results(result_dir, total_tasks=1)

        assert 0 not in results

    def test_header_only_csv_complete_via_tasks_dict(self, tmp_path):
        """Same contract as the check_results variant above, but driven
        through ``check_results_from_tasks`` (the per-task dict path)."""
        task_result_dir = tmp_path / "task0"
        task_result_dir.mkdir()
        (task_result_dir / "out.csv").write_text("a,b\n")
        tasks_data = {
            "total_tasks": 1,
            "tasks": {"0": {"result_dir": str(task_result_dir)}},
        }

        results = check_results_from_tasks(tasks_data, file_glob="*.csv")
        assert 0 in results

        strict = check_results_from_tasks(tasks_data, file_glob="*.csv", min_rows=1)
        assert 0 not in strict


# ─── Bug 1: report timestamps include explicit UTC offset ─────────────────


class TestReportTimestampIsUtc:
    def test_report_status_from_tasks_timestamp_has_offset(self, tmp_path):
        """Previously ``time.strftime("%Y-%m-%dT%H:%M:%S")`` emitted local
        time without a TZ marker; downstream consumers couldn't tell what
        timezone the timestamp was in.  The fix uses an explicit UTC ISO
        string so the field is unambiguous.
        """
        from hpc_agent.execution.mapreduce.reduce.status import report_status_from_tasks

        task_dir = tmp_path / "t0"
        task_dir.mkdir()
        tasks_data = {
            "total_tasks": 1,
            "tasks": {"0": {"result_dir": str(task_dir)}},
        }
        report = report_status_from_tasks(tasks_data, [], scheduler="slurm")
        assert report["timestamp"].endswith("+00:00")

    def test_report_status_timestamp_has_offset(self, tmp_path):
        result_dir = tmp_path / "r"
        result_dir.mkdir()
        report = report_status(result_dir, total_tasks=0, job_ids=[], scheduler="slurm")
        assert report["timestamp"].endswith("+00:00")


# ─── Bug 13: detect_scheduler honours experiment_meta.json hint ──────────


class TestDetectSchedulerMetaHint:
    def test_meta_file_overrides_local_sacct_heuristic(self, tmp_path, monkeypatch):
        """When ``sacct --version`` is unavailable locally, the auto-
        detector previously fell through to ``"sge"`` regardless of the
        actual cluster type.  The fix walks the result_dir up to the
        experiment root looking for ``experiment_meta.json`` so a Slurm
        cluster's meta file wins over a missing local ``sacct``.
        """
        from hpc_agent.execution.mapreduce.reduce.status import detect_scheduler

        # Place the meta file at the experiment root, not the per-task dir.
        exp = tmp_path / "exp"
        task = exp / "task0"
        task.mkdir(parents=True)
        (exp / "experiment_meta.json").write_text('{"backend": "slurm"}')

        # Pretend sacct isn't on PATH so the heuristic would say SGE.
        def no_sacct(*args, **kwargs):
            raise FileNotFoundError("no sacct")

        with patch(
            "hpc_agent.execution.mapreduce.reduce.status.subprocess.run",
            side_effect=no_sacct,
        ):
            assert detect_scheduler(task) == "slurm"

    def test_falls_back_to_sge_when_no_meta_and_no_sacct(self, tmp_path):
        """No meta file, no sacct → preserve the existing fallback."""
        from hpc_agent.execution.mapreduce.reduce.status import detect_scheduler

        def no_sacct(*args, **kwargs):
            raise FileNotFoundError("no sacct")

        with patch(
            "hpc_agent.execution.mapreduce.reduce.status.subprocess.run",
            side_effect=no_sacct,
        ):
            assert detect_scheduler(tmp_path) == "sge"

    def test_meta_file_recognizes_pbspro_hint(self, tmp_path):
        """A ``pbspro`` backend hint resolves to ``pbspro`` (contains neither
        "sge" nor "slurm", so the new fork branches must catch it)."""
        from hpc_agent.execution.mapreduce.reduce.status import detect_scheduler

        exp = tmp_path / "exp"
        task = exp / "task0"
        task.mkdir(parents=True)
        (exp / "experiment_meta.json").write_text('{"backend": "pbspro"}')
        assert detect_scheduler(task) == "pbspro"

    def test_meta_file_recognizes_torque_hint(self, tmp_path):
        from hpc_agent.execution.mapreduce.reduce.status import detect_scheduler

        exp = tmp_path / "exp_t"
        task = exp / "task0"
        task.mkdir(parents=True)
        (exp / "experiment_meta.json").write_text('{"backend": "torque"}')
        assert detect_scheduler(task) == "torque"

    def test_report_status_from_tasks_uses_first_task_meta(self, tmp_path, monkeypatch):
        """``report_status_from_tasks`` previously called
        ``detect_scheduler()`` with no args, bypassing every meta hint.
        Now it pulls a representative result_dir from the first task so
        the heuristic has a chance to read the experiment meta JSON
        sitting at the experiment root.
        """
        from hpc_agent.execution.mapreduce.reduce.status import report_status_from_tasks

        exp = tmp_path / "exp2"
        task = exp / "t0"
        task.mkdir(parents=True)
        (exp / "experiment_meta.json").write_text('{"backend": "slurm"}')

        tasks_data = {
            "total_tasks": 1,
            "tasks": {"0": {"result_dir": str(task)}},
        }

        def no_sacct(*args, **kwargs):
            raise FileNotFoundError("no sacct")

        with patch(
            "hpc_agent.execution.mapreduce.reduce.status.subprocess.run",
            side_effect=no_sacct,
        ):
            report = report_status_from_tasks(tasks_data, [])
        assert report["scheduler"] == "slurm"


# ─── BUG 3: status reporter must not let a foreign tasks.py wedge the report ──


class TestTemplateNeedsResolveKwargs:
    """``result_dir_template`` placeholder analysis gates the tasks.py import."""

    def test_run_id_task_id_only_needs_no_kwargs(self):
        from hpc_agent.execution.mapreduce.reduce.status import _template_needs_resolve_kwargs

        assert _template_needs_resolve_kwargs("results/{run_id}/task_{task_id}") is False
        assert _template_needs_resolve_kwargs("out/{task_id}") is False
        assert _template_needs_resolve_kwargs("flat") is False  # no placeholders at all

    def test_other_placeholder_needs_kwargs(self):
        from hpc_agent.execution.mapreduce.reduce.status import _template_needs_resolve_kwargs

        assert _template_needs_resolve_kwargs("results/{model}/task_{task_id}") is True
        assert _template_needs_resolve_kwargs("results/{run_id}/{horizon}/{task_id}") is True

    def test_unparseable_template_is_conservative(self):
        """A malformed template falls through to the import path (prior behavior)."""
        from hpc_agent.execution.mapreduce.reduce.status import _template_needs_resolve_kwargs

        assert _template_needs_resolve_kwargs("results/{unclosed") is True


class TestBuildPerTaskDictDegraded:
    """``_build_per_task_dict_from_sidecar`` tolerates ``tasks_module=None``."""

    def test_none_module_synthesizes_from_run_id_task_id(self):
        from hpc_agent.execution.mapreduce.reduce.status import _build_per_task_dict_from_sidecar

        sidecar = {
            "task_count": 2,
            "result_dir_template": "results/{run_id}/task_{task_id}",
            "run_id": "abc123",
            "wave_map": {},
            "cmd_sha": "deadbeef",
        }
        out = _build_per_task_dict_from_sidecar(sidecar, None)

        assert out["total_tasks"] == 2
        assert out["tasks"]["0"]["result_dir"] == "results/abc123/task_0"
        assert out["tasks"]["1"]["result_dir"] == "results/abc123/task_1"
        # Degraded path carries empty params (grid rollup falls under "_").
        assert out["tasks"]["0"]["params"] == {}

    def test_none_module_with_kwargs_template_yields_empty_dir_not_crash(self):
        """A kwargs template run degraded formats with an empty context — the
        missing field leaves an empty ``result_dir`` rather than raising."""
        from hpc_agent.execution.mapreduce.reduce.status import _build_per_task_dict_from_sidecar

        sidecar = {
            "task_count": 1,
            "result_dir_template": "results/{model}/task_{task_id}",
            "run_id": "r1",
            "wave_map": {},
        }
        out = _build_per_task_dict_from_sidecar(sidecar, None)
        assert out["tasks"]["0"]["result_dir"] == ""


def _drive_main(tmp_path, monkeypatch, sidecar, *, run_id="run1"):
    """Drive ``status._main`` with a fake sidecar + a real ``.hpc/tasks.py``.

    Returns ``(exit_code, parsed_stdout_doc)``. ``report_status_from_tasks`` is
    patched to a trivial stub so no scheduler subprocess runs; the only thing
    under test is the import-gating / degrade logic around it.
    """
    from hpc_agent.execution.mapreduce.reduce import status as status_mod

    hpc_dir = tmp_path / ".hpc"
    hpc_dir.mkdir()
    # A poison-pill tasks.py: importing it raises at module top-level, modeling
    # a foreign campaign's strategy file that needs env vars THIS run never set.
    (hpc_dir / "tasks.py").write_text(
        "raise RuntimeError('foreign campaign tasks.py cannot import here')\n"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["status", "--run-id", run_id])
    monkeypatch.setattr(status_mod, "read_run_sidecar", lambda _exp, _rid: sidecar, raising=False)
    # read_run_sidecar is imported lazily inside _main via
    # ``from hpc_agent.state.runs import read_run_sidecar`` — patch the source.
    monkeypatch.setattr(
        "hpc_agent.state.runs.read_run_sidecar", lambda _exp, _rid: sidecar, raising=False
    )

    def _stub_report(tasks_data, job_ids, **kwargs):
        return {
            "total_tasks": tasks_data["total_tasks"],
            "tasks": {tid: {"status": "unknown"} for tid in tasks_data["tasks"]},
            "summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
            "errors": [],
        }

    monkeypatch.setattr(status_mod, "report_status_from_tasks", _stub_report)
    monkeypatch.setattr(status_mod, "rollup_by_grid_point", lambda *a, **k: {})
    monkeypatch.setattr(status_mod, "rollup_by_wave", lambda *a, **k: {})

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = status_mod._main()
    return rc, json.loads(buf.getvalue())


class TestMainImportGating:
    """``_main`` imports ``tasks.py`` lazily and degrades on failure (BUG 3)."""

    def test_no_kwargs_template_skips_import_entirely(self, tmp_path, monkeypatch):
        """A run whose template needs no resolve() kwargs reconciles even when
        ``load_tasks_module`` would explode — the import is never attempted."""
        sidecar = {
            "task_count": 2,
            "result_dir_template": "results/{run_id}/task_{task_id}",
            "run_id": "run1",
            "wave_map": {},
        }

        def _boom(*_a, **_k):
            raise AssertionError("load_tasks_module must NOT be called for a no-kwargs template")

        # Patch the source binding (imported lazily inside _main).
        with patch("hpc_agent.load_tasks_module", side_effect=_boom):
            rc, doc = _drive_main(tmp_path, monkeypatch, sidecar)

        assert rc == 0
        # No degrade note: the import was skipped, not failed.
        codes = [e["code"] for e in doc.get("errors", [])]
        assert "tasks_py_import_error" not in codes
        assert "tasks_py_import_degraded" not in codes

    def test_kwargs_template_import_failure_degrades_not_fails(self, tmp_path, monkeypatch):
        """A run whose template DOES need kwargs but whose tasks.py won't import
        degrades to task_id-only result dirs (exit 0 + non-fatal note) instead
        of returning ``tasks_py_import_error`` / exit 2 for the whole run."""
        sidecar = {
            "task_count": 1,
            "result_dir_template": "results/{model}/task_{task_id}",
            "run_id": "run1",
            "wave_map": {},
        }
        # The on-disk .hpc/tasks.py (written by _drive_main) raises on import,
        # so the real load_tasks_module fails — exactly the BUG 3 scenario.
        rc, doc = _drive_main(tmp_path, monkeypatch, sidecar)

        assert rc == 0  # degraded, NOT a hard exit-2 failure
        codes = [e["code"] for e in doc.get("errors", [])]
        assert "tasks_py_import_degraded" in codes
        assert "tasks_py_import_error" not in codes
