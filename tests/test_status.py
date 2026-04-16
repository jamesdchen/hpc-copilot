"""Tests for hpc_mapreduce.reduce.status — check_results and report_status."""

from __future__ import annotations

from unittest.mock import patch

from hpc_mapreduce.reduce.status import (
    check_results,
    check_results_from_manifest,
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

        # Only task 1 should be found; task 2 in _wip_ should be skipped
        assert 1 in results
        assert 2 not in results
        assert len(results) == 1


class TestReportStatus:
    def test_report_status_basic(self, tmp_path):
        # Patch scheduler query functions to avoid real subprocess calls.
        with patch("hpc_mapreduce.reduce.status.detect_scheduler", return_value="slurm"), \
             patch("hpc_mapreduce.infra.backends.query.query_sacct", return_value={}):
            result = report_status(
                result_dir=tmp_path,
                job_ids=["12345"],
                total_tasks=1,
                scheduler="slurm",
            )

        assert "total_tasks" in result
        assert result["total_tasks"] == 1
        assert "summary" in result


class TestHeaderOnlyCsv:
    """Header-only CSVs should count as complete by default (P1.4 bug fix).

    A legitimately-empty result (e.g. a backtest period with zero trades) used
    to be marked failed and trigger infinite auto-resubmit in ``/monitor``.
    The default is now non-zero byte = complete; callers opt into the stricter
    check with ``min_rows>0``.
    """

    def test_header_only_csv_complete_by_default(self, tmp_path):
        result_dir = tmp_path / "results"
        task_dir = result_dir / "task_1"
        task_dir.mkdir(parents=True)
        (task_dir / "out.csv").write_text("col_a,col_b\n")  # header only

        results = check_results(result_dir, total_tasks=1)

        assert 1 in results
        assert results[1]["status"] == "complete"

    def test_header_only_csv_incomplete_with_min_rows(self, tmp_path):
        result_dir = tmp_path / "results"
        task_dir = result_dir / "task_1"
        task_dir.mkdir(parents=True)
        (task_dir / "out.csv").write_text("col_a,col_b\n")  # header only

        results = check_results(result_dir, total_tasks=1, min_rows=1)

        assert 1 not in results

    def test_zero_byte_file_still_incomplete(self, tmp_path):
        """A truly empty (zero-byte) file is still treated as incomplete."""
        result_dir = tmp_path / "results"
        task_dir = result_dir / "task_1"
        task_dir.mkdir(parents=True)
        (task_dir / "out.csv").write_text("")

        results = check_results(result_dir, total_tasks=1)

        assert 1 not in results

    def test_manifest_header_only_csv_complete_by_default(self, tmp_path):
        task_result_dir = tmp_path / "task0"
        task_result_dir.mkdir()
        (task_result_dir / "out.csv").write_text("a,b\n")
        manifest = {
            "total_tasks": 1,
            "tasks": {"0": {"result_dir": str(task_result_dir)}},
        }

        results = check_results_from_manifest(manifest, file_glob="*.csv")
        assert 1 in results

        strict = check_results_from_manifest(manifest, file_glob="*.csv", min_rows=1)
        assert 1 not in strict
