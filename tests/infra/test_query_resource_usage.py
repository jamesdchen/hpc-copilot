"""Tests for the resource-usage fields emitted by query_sacct / query_sge.

Feeds canned ``sacct --format=...`` and ``qstat`` / ``qacct`` output into the
batched query functions and asserts ``elapsed_s``, ``cpu_s``, ``gpu_s``
land on each task dict.  Permissive-parser contract: unrecognized GPU
strings fall back to 0 rather than crashing.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from hpc_agent.execution.mapreduce.reduce.metrics import reduce_resource_usage
from hpc_agent.infra.backends import query as qmod
from hpc_agent.infra.backends.query import (
    parse_gpu_count_from_sge_resources,
    parse_gpu_count_from_tres,
)


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# GPU-parser unit tests (permissive)
# ---------------------------------------------------------------------------


class TestParseGpuFromTres:
    def test_gres_gpu_plain(self):
        assert parse_gpu_count_from_tres("cpu=4,mem=16G,gres/gpu=2") == 2

    def test_gres_gpu_typed(self):
        assert parse_gpu_count_from_tres("gres/gpu:a100=1,cpu=8") == 1

    def test_sum_multiple(self):
        assert parse_gpu_count_from_tres("gres/gpu:a100=1,gres/gpu:h100=2") == 3

    def test_no_gpu(self):
        assert parse_gpu_count_from_tres("cpu=8,mem=32G") == 0

    def test_empty_returns_zero(self):
        assert parse_gpu_count_from_tres("") == 0

    def test_garbage_returns_zero(self):
        # Permissive: don't crash on unrecognized shapes.
        assert parse_gpu_count_from_tres("gres/gpu=abc,nonsense") == 0


class TestParseGpuFromSge:
    def test_gpu_eq_n(self):
        assert parse_gpu_count_from_sge_resources("h_rt=3600,gpu=2,mem=16G") == 2

    def test_gpu_in_qsub_arg_list(self):
        assert parse_gpu_count_from_sge_resources("-l h_rt=3600 -l gpu=1 -q gpu.q") == 1

    def test_no_gpu_returns_zero(self):
        assert parse_gpu_count_from_sge_resources("h_rt=3600,mem=16G") == 0

    def test_empty_returns_zero(self):
        assert parse_gpu_count_from_sge_resources("") == 0

    def test_does_not_match_num_gpu_prefix(self):
        # We only match bare 'gpu=N' (not arbitrary prefixes) to avoid FPs.
        assert parse_gpu_count_from_sge_resources("num_cpu=2,total=3") == 0


# ---------------------------------------------------------------------------
# query_sacct: extended --format and derived fields
# ---------------------------------------------------------------------------


class TestSacctResourceUsage:
    def test_format_string_requests_new_columns(self, monkeypatch):
        captured: list[list[str]] = []

        def responder(cmd, *a, **kw):
            captured.append(list(cmd))
            return _cp(stdout="")

        monkeypatch.setattr(subprocess, "run", responder)
        qmod.query_sacct(["999"])

        assert len(captured) == 1
        argv = captured[0]
        fmt = next(a for a in argv if a.startswith("--format="))
        assert "ElapsedRaw" in fmt
        assert "ReqCPUS" in fmt
        assert "AllocTRES" in fmt

    def test_derived_fields_cpu_and_gpu(self, monkeypatch):
        # One GPU task and one CPU-only task.
        stdout = (
            "100_1|COMPLETED|0:0|3600|8|cpu=8,mem=32G,gres/gpu=1\n"
            "100_2|COMPLETED|0:0|600|4|cpu=4,mem=16G\n"
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))
        out = qmod.query_sacct(["100"])
        # ArrayIndex 1/2 ingest to HpcTaskId 0/1.
        t1 = out["tasks"][0]
        assert t1["elapsed_s"] == 3600
        assert t1["cpu_s"] == 3600 * 8
        assert t1["gpu_s"] == 3600 * 1

        t2 = out["tasks"][1]
        assert t2["elapsed_s"] == 600
        assert t2["cpu_s"] == 600 * 4
        assert t2["gpu_s"] == 0

    def test_missing_columns_default_to_zero(self, monkeypatch):
        # Only 3 columns — older sacct / test fixtures that haven't been updated.
        stdout = "999_1|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))
        out = qmod.query_sacct(["999"])
        t = out["tasks"][0]  # ArrayIndex 1 -> HpcTaskId 0
        assert t["elapsed_s"] == 0
        assert t["cpu_s"] == 0
        assert t["gpu_s"] == 0


# ---------------------------------------------------------------------------
# query_sge: qacct-derived resource usage
# ---------------------------------------------------------------------------


class TestSgeResourceUsage:
    def test_qacct_ru_wallclock_and_slots(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="")
            block = (
                "==============================================================\n"
                f"jobnumber    {cmd[-1]}\n"
                "taskid       1\n"
                "slots        4\n"
                "ru_wallclock 900\n"
                "exit_status  0\n"
                "failed       0\n"
            )
            return _cp(stdout=block)

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["42"], user="u")
        t = out["tasks"][0]  # taskid 1 (ArrayIndex) -> HpcTaskId 0
        assert t["elapsed_s"] == 900
        assert t["cpu_s"] == 900 * 4
        assert t["gpu_s"] == 0

    def test_qacct_gpu_from_hard_resource_list(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="")
            block = (
                "==============================================================\n"
                f"jobnumber    {cmd[-1]}\n"
                "taskid       7\n"
                "slots        2\n"
                "ru_wallclock 1200\n"
                "hard         resource_list h_rt=7200,gpu=1\n"
                "exit_status  0\n"
                "failed       0\n"
            )
            return _cp(stdout=block)

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["42"], user="u")
        t = out["tasks"][6]  # taskid 7 (ArrayIndex) -> HpcTaskId 6
        assert t["elapsed_s"] == 1200
        assert t["cpu_s"] == 1200 * 2
        assert t["gpu_s"] == 1200 * 1

    def test_qstat_running_task_has_zero_usage_keys(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(
                    stdout=(
                        "job-ID prior name user state submit/start queue slots ja-task-ID\n"
                        "500 0.5 myjob u r 04/17/2026 12:00:00 all.q 1 1-2:1\n"
                    )
                )
            return _cp(stdout="", returncode=1)

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["500"], user="u")
        t = out["tasks"][1]
        # State is active; cost fields are present and zero (shape is uniform).
        assert t["state"] == "RUNNING"
        assert t["elapsed_s"] == 0
        assert t["cpu_s"] == 0
        assert t["gpu_s"] == 0


# ---------------------------------------------------------------------------
# reduce_resource_usage
# ---------------------------------------------------------------------------


class TestReduceResourceUsage:
    def test_sums_hours(self):
        tasks = {
            "1": {"elapsed_s": 3600, "cpu_s": 8 * 3600, "gpu_s": 3600},
            "2": {"elapsed_s": 1800, "cpu_s": 4 * 1800, "gpu_s": 0},
            "3": {"elapsed_s": 0, "cpu_s": 0, "gpu_s": 0},
        }
        out = reduce_resource_usage(tasks)
        assert out["cpu_hours"] == round((8 * 3600 + 4 * 1800) / 3600.0, 4)
        assert out["gpu_hours"] == round(3600 / 3600.0, 4)
        assert out["elapsed_hours"] == round((3600 + 1800) / 3600.0, 4)
        # Only the two tasks with elapsed_s > 0 count.
        assert out["tasks_counted"] == 2

    def test_empty_input(self):
        out = reduce_resource_usage({})
        assert out == {
            "cpu_hours": 0.0,
            "gpu_hours": 0.0,
            "elapsed_hours": 0.0,
            "tasks_counted": 0,
        }

    def test_missing_keys_permissive(self):
        # Mix of tasks with / without cost fields (e.g. still pending).
        tasks = {"1": {"status": "pending"}, "2": {"elapsed_s": 60, "cpu_s": 60, "gpu_s": 0}}
        out = reduce_resource_usage(tasks)
        assert out["tasks_counted"] == 1
        assert out["elapsed_hours"] == round(60 / 3600.0, 4)

    def test_non_dict_values_ignored(self):
        # Hostile input: values that aren't dicts shouldn't blow up.
        tasks = {"1": "not a dict", "2": {"elapsed_s": 3600, "cpu_s": 3600, "gpu_s": 0}}
        out = reduce_resource_usage(tasks)  # type: ignore[arg-type]
        assert out["tasks_counted"] == 1
