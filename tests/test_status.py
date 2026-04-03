"""Tests for hpc_mapreduce.reduce.status — reduce_counters and report_status."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hpc_mapreduce.reduce.status import reduce_counters, report_status


class TestReduceCounters:
    def test_reduce_counters_basic(self, tmp_path):
        for i, (rp, tr) in enumerate([(100, 500), (200, 500), (150, 500)], start=1):
            (tmp_path / f"_counters_{i}.json").write_text(
                json.dumps({"rows_processed": rp, "total_rows": tr})
            )
        result = reduce_counters(tmp_path, 3)
        assert result["reporting"] == 3
        assert result["totals"]["rows_processed"] == 450
        assert result["totals"]["total_rows"] == 1500
        assert result["progress"] == pytest.approx(450 / 1500)

    def test_reduce_counters_missing_files(self, tmp_path):
        (tmp_path / "_counters_2.json").write_text(
            json.dumps({"rows_processed": 75, "total_rows": 200})
        )
        result = reduce_counters(tmp_path, 3)
        assert result["reporting"] == 1
        assert result["totals"]["rows_processed"] == 75
        assert result["totals"]["total_rows"] == 200

    def test_reduce_counters_corrupt_file(self, tmp_path):
        (tmp_path / "_counters_1.json").write_text(
            json.dumps({"rows_processed": 50, "total_rows": 100})
        )
        (tmp_path / "_counters_2.json").write_text("NOT VALID JSON {{{")
        (tmp_path / "_counters_3.json").write_text(
            json.dumps({"rows_processed": 30, "total_rows": 100})
        )
        result = reduce_counters(tmp_path, 3)
        assert result["reporting"] == 2
        assert result["totals"]["rows_processed"] == 80
        assert result["totals"]["total_rows"] == 200

    def test_reduce_counters_empty_dir(self, tmp_path):
        result = reduce_counters(tmp_path, 3)
        assert result["reporting"] == 0
        assert result["totals"] == {}
        assert result["progress"] is None

    def test_reduce_counters_no_progress_keys(self, tmp_path):
        for i in range(1, 4):
            (tmp_path / f"_counters_{i}.json").write_text(
                json.dumps({"cache_hits": 10, "retries": 2})
            )
        result = reduce_counters(tmp_path, 3)
        assert result["progress"] is None
        assert result["totals"]["cache_hits"] == 30
        assert result["totals"]["retries"] == 6

    def test_report_status_includes_counters(self, tmp_path):
        # Write counter files so reduce_counters finds them.
        (tmp_path / "_counters_1.json").write_text(
            json.dumps({"rows_processed": 10, "total_rows": 50})
        )

        # Patch scheduler query functions to avoid real subprocess calls.
        with patch("hpc_mapreduce.reduce.status.detect_scheduler", return_value="slurm"), \
             patch("hpc_mapreduce.infra.backends.query.query_sacct", return_value={}):
            result = report_status(
                result_dir=tmp_path,
                job_ids=["12345"],
                total_chunks=1,
                scheduler="slurm",
                include_counters=True,
            )

        assert "counters" in result
        assert result["counters"]["reporting"] == 1
        assert result["counters"]["totals"]["rows_processed"] == 10
