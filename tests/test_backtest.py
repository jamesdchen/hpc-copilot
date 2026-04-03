"""Tests for expand_backtest() and build_task_manifest() with backtest."""

from __future__ import annotations

from datetime import date, datetime

from hpc_mapreduce.job.grid import build_task_manifest, expand_backtest


class TestExpandBacktest:
    def test_6m_over_5_years(self):
        """6M duration over 2020-01-01 to 2024-12-31 → 10 periods."""
        periods = expand_backtest({
            "start": "2020-01-01",
            "end": "2024-12-31",
            "chunk_duration": "6M",
        })
        assert len(periods) == 10
        # First period
        assert periods[0]["start"] == "2020-01-01"
        assert periods[0]["end"] == "2020-06-30"
        # Last period
        assert periods[-1]["start"] == "2024-07-01"
        assert periods[-1]["end"] == "2024-12-31"

    def test_1y_over_3_years(self):
        """1Y duration over 3 years → 3 periods."""
        periods = expand_backtest({
            "start": "2020-01-01",
            "end": "2022-12-31",
            "chunk_duration": "1Y",
        })
        assert len(periods) == 3
        assert periods[0]["start"] == "2020-01-01"
        assert periods[0]["end"] == "2020-12-31"
        assert periods[1]["start"] == "2021-01-01"
        assert periods[1]["end"] == "2021-12-31"
        assert periods[2]["start"] == "2022-01-01"
        assert periods[2]["end"] == "2022-12-31"

    def test_30d_over_90_days(self):
        """30D duration over ~90 days → 3 periods."""
        periods = expand_backtest({
            "start": "2023-01-01",
            "end": "2023-03-31",
            "chunk_duration": "30D",
        })
        assert len(periods) == 3
        assert periods[0]["start"] == "2023-01-01"
        assert periods[0]["end"] == "2023-01-30"
        assert periods[1]["start"] == "2023-01-31"
        assert periods[1]["end"] == "2023-03-01"
        assert periods[2]["start"] == "2023-03-02"
        assert periods[2]["end"] == "2023-03-31"

    def test_partial_final_period(self):
        """End date doesn't align with chunk boundary → last period is shorter."""
        periods = expand_backtest({
            "start": "2023-01-01",
            "end": "2023-02-15",
            "chunk_duration": "1M",
        })
        assert len(periods) == 2
        assert periods[0]["start"] == "2023-01-01"
        assert periods[0]["end"] == "2023-01-31"
        assert periods[1]["start"] == "2023-02-01"
        assert periods[1]["end"] == "2023-02-15"

    def test_custom_arg_names(self):
        """Custom start_arg/end_arg produce correct dict keys."""
        periods = expand_backtest({
            "start": "2023-01-01",
            "end": "2023-06-30",
            "chunk_duration": "6M",
            "start_arg": "--begin",
            "end_arg": "--until",
        })
        assert len(periods) == 1
        assert periods[0]["begin"] == "2023-01-01"
        assert periods[0]["until"] == "2023-06-30"

    def test_no_gaps_no_overlaps(self):
        """Period ends are day-before-next-start (no gaps, no overlaps)."""
        periods = expand_backtest({
            "start": "2020-01-01",
            "end": "2024-12-31",
            "chunk_duration": "6M",
        })
        for i in range(len(periods) - 1):
            end = date.fromisoformat(periods[i]["end"])
            next_start = date.fromisoformat(periods[i + 1]["start"])
            assert (next_start - end).days == 1, (
                f"Gap/overlap between period {i} end ({end}) and "
                f"period {i+1} start ({next_start})"
            )

    def test_hours_duration(self):
        """2h duration over 6 hours → 3 periods with datetime boundaries."""
        periods = expand_backtest({
            "start": "2023-06-15T08:00:00",
            "end": "2023-06-15T13:59:59",
            "chunk_duration": "2h",
        })
        assert len(periods) == 3
        assert periods[0]["start"] == "2023-06-15T08:00:00"
        assert periods[0]["end"] == "2023-06-15T09:59:59"
        assert periods[1]["start"] == "2023-06-15T10:00:00"
        assert periods[1]["end"] == "2023-06-15T11:59:59"
        assert periods[2]["start"] == "2023-06-15T12:00:00"
        assert periods[2]["end"] == "2023-06-15T13:59:59"

    def test_minutes_duration(self):
        """30m duration over 90 minutes → 3 periods."""
        periods = expand_backtest({
            "start": "2023-06-15T09:00:00",
            "end": "2023-06-15T10:29:59",
            "chunk_duration": "30m",
        })
        assert len(periods) == 3
        assert periods[0]["start"] == "2023-06-15T09:00:00"
        assert periods[0]["end"] == "2023-06-15T09:29:59"
        assert periods[1]["start"] == "2023-06-15T09:30:00"
        assert periods[2]["start"] == "2023-06-15T10:00:00"
        assert periods[2]["end"] == "2023-06-15T10:29:59"

    def test_sub_daily_no_gaps(self):
        """Sub-daily periods have no gaps (1 second between end and next start)."""
        periods = expand_backtest({
            "start": "2023-06-15T00:00:00",
            "end": "2023-06-15T05:59:59",
            "chunk_duration": "2h",
        })
        for i in range(len(periods) - 1):
            end = datetime.fromisoformat(periods[i]["end"])
            next_start = datetime.fromisoformat(periods[i + 1]["start"])
            assert (next_start - end).total_seconds() == 1


class TestBuildTaskManifestWithBacktest:
    def test_grid_times_backtest(self):
        """Grid of 2 combos × backtest of 3 periods → 6 tasks."""
        grid = {"alpha": [0.1, 0.2], "beta": [1]}
        backtest = {
            "start": "2020-01-01",
            "end": "2022-12-31",
            "chunk_duration": "1Y",
        }
        manifest = build_task_manifest(
            run_cmd="python train.py",
            grid=grid,
            result_dir_template="/results/{run_id}",
            backtest=backtest,
        )
        assert manifest["total_tasks"] == 6
        assert manifest["grid_size"] == 2
        assert len(manifest["tasks"]) == 6

    def test_start_end_in_task_commands(self):
        """Start/end dates appear in the task command strings."""
        grid = {"lr": [0.01]}
        backtest = {
            "start": "2023-01-01",
            "end": "2023-06-30",
            "chunk_duration": "6M",
        }
        manifest = build_task_manifest(
            run_cmd="python train.py",
            grid=grid,
            result_dir_template="/results/{run_id}",
            backtest=backtest,
        )
        task = manifest["tasks"]["0"]
        assert "--start 2023-01-01" in task["cmd"]
        assert "--end 2023-06-30" in task["cmd"]

    def test_result_dir_includes_params(self):
        """result_dir contains the run_id derived from params."""
        grid = {"lr": [0.01]}
        manifest = build_task_manifest(
            run_cmd="python train.py",
            grid=grid,
            result_dir_template="/results/{run_id}",
        )
        task = manifest["tasks"]["0"]
        assert "0.01" in task["result_dir"]

    def test_without_backtest(self):
        """Without backtest, works as pure grid expansion."""
        grid = {"x": [1, 2], "y": ["a", "b"]}
        manifest = build_task_manifest(
            run_cmd="python run.py",
            grid=grid,
            result_dir_template="/out/{run_id}",
        )
        assert manifest["total_tasks"] == 4
        assert len(manifest["tasks"]) == 4
        # No period key in tasks
        for task in manifest["tasks"].values():
            assert "period" not in task
