"""Tests for hpc_agent.execution.mapreduce.reduce.status — check_results and report_status."""

from __future__ import annotations

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
