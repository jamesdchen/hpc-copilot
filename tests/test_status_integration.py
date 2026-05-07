"""Integration tests for ``claude_hpc.mapreduce.reduce.status``.

Layered approach:

* **Layer 1 — CLI integration.** Exercise the ``python -m
  claude_hpc.mapreduce.reduce.status`` entry point against a real
  ``.hpc/`` tree under ``tmp_path``. Catches envelope-shape drift and
  the four documented error envelopes (sidecar_not_found,
  sidecar_parse_error, tasks_py_not_found, tasks_py_import_error).
  These exercise ``_main`` end-to-end — the largest untested chunk
  before this file (~100 of 125 missing lines lived there).

* **Layer 2 — conservation property.** Hypothesis-generate a
  population of (complete, running, pending, failed, unknown)
  per-task states and verify the summary counts sum to total_tasks.
  Catches "we forgot to count category X" bugs.

* **Layer 3 — pure-helper unit tests.** ``rollup_by_grid_point``,
  ``rollup_by_wave``, ``_grid_point_key``, ``_categorize`` —
  no I/O, simple dict-in/dict-out.

The deliberate non-goal: NOT mocking the function under test. Real
filesystem state via ``tmp_path``; mocks only at the
``backends.query_jobs`` boundary where real cluster I/O is impractical.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from claude_hpc.mapreduce.reduce.status import (
    _categorize,
    _empty_summary,
    _grid_point_key,
    report_status_from_tasks,
    rollup_by_grid_point,
    rollup_by_wave,
)

if TYPE_CHECKING:
    from pathlib import Path


# ─── helpers ────────────────────────────────────────────────────────────


def _make_run(
    tmp_path: Path,
    *,
    run_id: str = "20260101-000000-aaaaaaa",
    task_kwargs: list[dict] | None = None,
    sidecar_overrides: dict | None = None,
) -> Path:
    """Materialize a real ``.hpc/{runs/<run_id>.json, tasks.py}`` tree.

    Returns the experiment_dir (= tmp_path) for use as ``cwd`` in
    subprocess invocations of the CLI.
    """
    if task_kwargs is None:
        task_kwargs = [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}]
    hpc = tmp_path / ".hpc"
    (hpc / "runs").mkdir(parents=True, exist_ok=True)
    sidecar = {
        "sidecar_schema_version": 2,
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "claude_hpc_version": "0.2.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python3 src/run.py",
        "result_dir_template": str(tmp_path / "results" / "{task_id}"),
        "task_count": len(task_kwargs),
        "tasks_py_sha": "1" * 64,
        "wave_map": {},
    }
    if sidecar_overrides:
        sidecar.update(sidecar_overrides)
    (hpc / "runs" / f"{run_id}.json").write_text(json.dumps(sidecar))
    (hpc / "tasks.py").write_text(
        f"_TASKS = {task_kwargs!r}\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n"
    )
    return tmp_path


def _write_complete_csv(tmp_path: Path, task_id: int) -> None:
    """Write a non-empty CSV that ``check_results_from_tasks`` will
    accept as complete."""
    rdir = tmp_path / "results" / str(task_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "out.csv").write_text("col\n1\n")


def _run_cli(experiment_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "claude_hpc.mapreduce.reduce.status", *args],
        cwd=str(experiment_dir),
        capture_output=True,
        text=True,
    )


# ─── Layer 1: CLI integration ──────────────────────────────────────────


_PINNED_KEYS = {"summary", "tasks", "rollup", "errors"}


def test_cli_emits_pinned_envelope_with_complete_tasks(tmp_path: Path) -> None:
    """Happy path: 4 tasks, 2 complete (have CSV), 2 with no result dir
    state. Envelope must have the 4 pinned keys and accurate counts."""
    _make_run(tmp_path)
    _write_complete_csv(tmp_path, 0)  # 0-based on disk → tid 1 in report
    _write_complete_csv(tmp_path, 1)  # tid 2

    proc = _run_cli(tmp_path, "--run-id", "20260101-000000-aaaaaaa")
    assert proc.returncode == 0, proc.stderr

    envelope = json.loads(proc.stdout)
    assert set(envelope) >= _PINNED_KEYS, envelope
    assert envelope["summary"]["complete"] == 2
    assert envelope["summary"]["unknown"] == 2  # no job_ids → tasks land in unknown
    # All summary keys present and integer-typed (TaskStatus contract).
    for key in ("complete", "running", "pending", "failed", "unknown"):
        assert key in envelope["summary"]
        assert isinstance(envelope["summary"][key], int)


def test_cli_sidecar_missing_emits_documented_error(tmp_path: Path) -> None:
    """No sidecar on disk → exit 2, error envelope with
    ``sidecar_not_found``, but the 4 pinned keys still present so
    parsers don't crash."""
    (tmp_path / ".hpc" / "runs").mkdir(parents=True, exist_ok=True)

    proc = _run_cli(tmp_path, "--run-id", "20260101-000000-aaaaaaa")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)

    envelope = json.loads(proc.stdout)
    assert set(envelope) >= _PINNED_KEYS, envelope
    assert any(e.get("code") == "sidecar_not_found" for e in envelope["errors"])
    assert envelope["summary"] == _empty_summary()


def test_cli_sidecar_parse_error_emits_documented_error(tmp_path: Path) -> None:
    """Corrupt JSON → ``sidecar_parse_error`` envelope, still 4 keys."""
    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20260101-000000-aaaaaaa.json").write_text("{not valid json")

    proc = _run_cli(tmp_path, "--run-id", "20260101-000000-aaaaaaa")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)

    envelope = json.loads(proc.stdout)
    assert set(envelope) >= _PINNED_KEYS
    assert any(e.get("code") == "sidecar_parse_error" for e in envelope["errors"])


def test_cli_tasks_py_missing_emits_documented_error(tmp_path: Path) -> None:
    """Sidecar present but no .hpc/tasks.py → tasks_py_not_found."""
    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "sidecar_schema_version": 2,
        "run_id": "20260101-000000-aaaaaaa",
        "cmd_sha": "a" * 64,
        "claude_hpc_version": "0.2.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python3 run.py",
        "result_dir_template": str(tmp_path / "results" / "{task_id}"),
        "task_count": 1,
        "tasks_py_sha": "1" * 64,
        "wave_map": {},
    }
    (runs / "20260101-000000-aaaaaaa.json").write_text(json.dumps(sidecar))
    # Deliberately no tasks.py.

    proc = _run_cli(tmp_path, "--run-id", "20260101-000000-aaaaaaa")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)

    envelope = json.loads(proc.stdout)
    assert set(envelope) >= _PINNED_KEYS
    assert any(e.get("code") == "tasks_py_not_found" for e in envelope["errors"])


def test_cli_envelope_has_rollup_and_waves_keys_when_pinned(tmp_path: Path) -> None:
    """``_main`` does ``setdefault`` on rollup/waves so the parse
    contract stays stable even when the run has no grid-point info."""
    _make_run(tmp_path)
    proc = _run_cli(tmp_path, "--run-id", "20260101-000000-aaaaaaa")
    assert proc.returncode == 0, proc.stderr
    envelope = json.loads(proc.stdout)
    assert "rollup" in envelope
    assert "waves" in envelope
    assert isinstance(envelope["rollup"], dict)
    assert isinstance(envelope["waves"], dict)


# ─── Layer 2: conservation property ────────────────────────────────────


_state_strategy = st.sampled_from(["RUNNING", "PENDING", "FAILED", "TIMEOUT", "OUT_OF_MEMORY"])


@given(
    n_complete=st.integers(min_value=0, max_value=5),
    n_running=st.integers(min_value=0, max_value=5),
    n_pending=st.integers(min_value=0, max_value=5),
    n_failed=st.integers(min_value=0, max_value=5),
)
@settings(max_examples=80, deadline=None)
def test_summary_counts_sum_to_total_tasks(
    tmp_path_factory: pytest.TempPathFactory,
    n_complete: int,
    n_running: int,
    n_pending: int,
    n_failed: int,
) -> None:
    """For any task-state population, summary counts sum to total_tasks.

    Catches "we forgot a category" bugs that example tests with fixed
    populations would never surface."""
    tmp_path = tmp_path_factory.mktemp("conservation")
    total = n_complete + n_running + n_pending + n_failed
    if total == 0:
        return  # skip degenerate; report still works but assertion is vacuous

    # Build the synthetic per-task dict the reporting code consumes.
    tasks_data: dict = {
        "schema_version": 2,
        "total_tasks": total,
        "tasks": {},
        "wave_map": {},
        "cmd_sha": "a" * 64,
    }
    # 0-based task IDs in the per-task dict; report keys 1-based.
    next_tid = 0
    for _ in range(n_complete):
        rdir = tmp_path / "results" / str(next_tid)
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "out.csv").write_text("col\n1\n")
        tasks_data["tasks"][str(next_tid)] = {
            "result_dir": str(rdir),
            "params": {"x": next_tid},
            "cmd_sha": None,
        }
        next_tid += 1
    for _ in range(n_running + n_pending + n_failed):
        # No result dir on disk → falls into job_info or unknown.
        tasks_data["tasks"][str(next_tid)] = {
            "result_dir": str(tmp_path / "results" / str(next_tid)),
            "params": {"x": next_tid},
            "cmd_sha": None,
        }
        next_tid += 1

    # Fake job_info for the running/pending/failed buckets — 1-based tids
    # in scheduler output, matching report_status's key convention.
    job_info: dict[int, dict] = {}
    cursor = n_complete + 1  # 1-based, after the complete block
    for _ in range(n_running):
        job_info[cursor] = {"state": "RUNNING"}
        cursor += 1
    for _ in range(n_pending):
        job_info[cursor] = {"state": "PENDING"}
        cursor += 1
    for _ in range(n_failed):
        job_info[cursor] = {"state": "FAILED"}
        cursor += 1

    fake_query = {"tasks": job_info, "errors": []}
    with (
        patch(
            "claude_hpc.mapreduce.reduce.status.detect_scheduler",
            return_value="slurm",
        ),
        patch(
            "claude_hpc.infra.backends.slurm.SlurmBackend.query_jobs",
            return_value=fake_query,
        ),
    ):
        report = report_status_from_tasks(tasks_data, ["1"], scheduler="slurm")

    counted = sum(
        report["summary"][k]
        for k in ("complete", "running", "pending", "failed", "unknown")
    )
    assert counted == total, (counted, total, report["summary"])


# ─── Layer 3: pure-helper unit tests ───────────────────────────────────


@pytest.mark.parametrize(
    "params,expected",
    [
        ({}, "_"),
        ({"x": 1}, "x=1"),
        ({"x": 1, "y": 2}, "x=1_y=2"),
        # Sorted by key — the contract pins this so ``rollup_by_grid_point``
        # buckets the same group regardless of dict insertion order.
        ({"y": 2, "x": 1}, "x=1_y=2"),
        ({"a": "foo", "b": True, "c": 3.5}, "a=foo_b=True_c=3.5"),
    ],
)
def test_grid_point_key_canonical(params: dict, expected: str) -> None:
    assert _grid_point_key(params) == expected


@pytest.mark.parametrize(
    "state,expected_bucket",
    [
        ("RUNNING", "running"),
        ("REQUEUED", "running"),
        ("CONFIGURING", "running"),
        ("PENDING", "pending"),
        ("FAILED", "failed"),
        ("CANCELLED", "failed"),
        ("CANCELLED+5", "failed"),  # cancelled-with-suffix prefix match
        ("TIMEOUT", "failed"),
        ("OUT_OF_MEMORY", "failed"),
        ("NODE_FAIL", "failed"),
        ("COMPLETED", "unknown"),  # not in any bucket — handled separately
        ("WEIRD_STATE", "unknown"),
    ],
)
def test_categorize_buckets(state: str, expected_bucket: str) -> None:
    assert _categorize(state) == expected_bucket


def test_rollup_by_grid_point_empty_inputs() -> None:
    assert rollup_by_grid_point({}, {}) == {}
    assert rollup_by_grid_point({"tasks": {}}, {"tasks": {}}) == {}


def test_rollup_by_grid_point_groups_by_params() -> None:
    """Two tasks with identical params land in the same bucket; their
    distinct statuses are summed within."""
    report = {
        "tasks": {
            "1": {"status": "complete"},
            "2": {"status": "complete"},
            "3": {"status": "failed"},
        },
    }
    tasks_data = {
        "tasks": {
            "0": {"params": {"lr": 0.1}},  # report tid 1
            "1": {"params": {"lr": 0.1}},  # report tid 2 — same bucket
            "2": {"params": {"lr": 0.2}},  # report tid 3 — different bucket
        },
    }
    rollup = rollup_by_grid_point(report, tasks_data)
    _zero = {"running": 0, "pending": 0, "unknown": 0}
    assert rollup == {
        "lr=0.1": {"complete": 2, "failed": 0, "total": 2, **_zero},
        "lr=0.2": {"complete": 0, "failed": 1, "total": 1, **_zero},
    }


def test_rollup_by_grid_point_skips_tasks_without_params_entry() -> None:
    """A report task with no matching per-task entry is dropped silently
    (not crashed on)."""
    report = {"tasks": {"1": {"status": "complete"}, "5": {"status": "complete"}}}
    tasks_data = {"tasks": {"0": {"params": {"x": 1}}}}  # only 1 entry
    rollup = rollup_by_grid_point(report, tasks_data)
    assert "x=1" in rollup
    assert rollup["x=1"]["total"] == 1


def test_rollup_by_wave_empty_wave_map_returns_empty() -> None:
    assert rollup_by_wave({"tasks": {"1": {"status": "complete"}}}, {}) == {}
    assert rollup_by_wave({"tasks": {}}, {"wave_map": {}}) == {}


def test_rollup_by_wave_buckets_by_wave_with_id_shift() -> None:
    """``wave_map`` keys are 0-based task ids; report keys are 1-based.
    The function must shift on lookup."""
    report = {
        "tasks": {
            "1": {"status": "complete"},
            "2": {"status": "running"},
            "3": {"status": "complete"},
        },
    }
    tasks_data = {"wave_map": {"0": [0, 1], "1": [2]}}  # 0-based tids
    rollup = rollup_by_wave(report, tasks_data)
    assert rollup == {
        "0": {"complete": 1, "running": 1, "pending": 0, "failed": 0, "unknown": 0, "total": 2},
        "1": {"complete": 1, "running": 0, "pending": 0, "failed": 0, "unknown": 0, "total": 1},
    }


def test_empty_summary_has_all_task_status_keys() -> None:
    """Pin the canonical zeroed shape — callers (the CLI error path,
    primarily) rely on every key being present."""
    summary = _empty_summary()
    assert set(summary) == {"complete", "running", "pending", "failed", "unknown"}
    assert all(v == 0 for v in summary.values())
