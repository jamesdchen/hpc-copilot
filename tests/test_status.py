"""Tests for hpc_mapreduce.reduce.status — check_results and report_status."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hpc_mapreduce.reduce.status import check_results, report_status


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
