"""Tests for ``claude_hpc.mapreduce.reduce.history``: per-campaign sidecar
filtering, result-dir resolution, and per-iteration reduce."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from hpc_mapreduce.job.runs import write_run_sidecar
from claude_hpc.mapreduce.reduce.history import (
    find_sidecars_by_campaign,
    prior,
    result_dirs_for_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


def _common_required_kwargs(run_id: str, task_count: int = 1) -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        claude_hpc_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=task_count,
        tasks_py_sha="1" * 64,
    )


def _write_metrics(result_dir: Path, payload: dict) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "metrics.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# find_sidecars_by_campaign
# ---------------------------------------------------------------------------


def test_find_sidecars_filters_by_campaign_and_orders_oldest_first(
    tmp_path: Path,
) -> None:
    write_run_sidecar(
        tmp_path, **_common_required_kwargs("20260101-000000-aaaaaaa"), campaign_id="A"
    )
    time.sleep(0.01)
    write_run_sidecar(
        tmp_path, **_common_required_kwargs("20260101-000001-bbbbbbb"), campaign_id="B"
    )
    time.sleep(0.01)
    write_run_sidecar(
        tmp_path, **_common_required_kwargs("20260101-000002-ccccccc"), campaign_id="A"
    )

    matched = find_sidecars_by_campaign(tmp_path, "A")
    assert [s["run_id"] for s in matched] == [
        "20260101-000000-aaaaaaa",
        "20260101-000002-ccccccc",
    ]


def test_find_sidecars_skips_records_without_campaign(tmp_path: Path) -> None:
    write_run_sidecar(tmp_path, **_common_required_kwargs("20260101-000000-untagged"))
    matched = find_sidecars_by_campaign(tmp_path, "any")
    assert matched == []


def test_find_sidecars_returns_empty_for_empty_campaign_id(tmp_path: Path) -> None:
    write_run_sidecar(
        tmp_path, **_common_required_kwargs("20260101-000000-aaaaaaa"), campaign_id="A"
    )
    assert find_sidecars_by_campaign(tmp_path, "") == []


# ---------------------------------------------------------------------------
# result_dirs_for_sidecar
# ---------------------------------------------------------------------------


def test_result_dirs_resolves_run_id_and_task_id(tmp_path: Path) -> None:
    """Templates referencing only {run_id} and {task_id} resolve cleanly."""
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000000-aaaaaaa", task_count=3),
        campaign_id="A",
    )
    for i in range(3):
        _write_metrics(
            tmp_path / "results" / "20260101-000000-aaaaaaa" / f"task_{i}",
            {"loss": 1.0 / (i + 1), "n_samples": 10},
        )
    sidecar = find_sidecars_by_campaign(tmp_path, "A")[0]
    dirs = result_dirs_for_sidecar(tmp_path, sidecar)
    assert len(dirs) == 3
    for d in dirs:
        assert (d / "metrics.json").is_file()


def test_result_dirs_glob_substitutes_unknown_placeholders(tmp_path: Path) -> None:
    """Templates with user kwargs like {seed} get glob-substituted; matching
    dirs that contain metrics.json are picked up without loading tasks.py."""
    sidecar_kwargs = _common_required_kwargs("20260101-000000-glb00000", task_count=2)
    sidecar_kwargs["result_dir_template"] = "results/{run_id}/task_{task_id}/seed_{seed}"
    write_run_sidecar(tmp_path, **sidecar_kwargs, campaign_id="A")
    # Only the user's tasks.py knows the actual seed values; we lay down two
    # candidate dirs per task and assert glob picks them up.
    for i in range(2):
        for seed in (42, 1337):
            _write_metrics(
                tmp_path / "results" / "20260101-000000-glb00000" / f"task_{i}" / f"seed_{seed}",
                {"loss": 0.1, "n_samples": 1},
            )
    sidecar = find_sidecars_by_campaign(tmp_path, "A")[0]
    dirs = result_dirs_for_sidecar(tmp_path, sidecar)
    assert len(dirs) == 4


def test_result_dirs_returns_empty_when_no_metrics_present(tmp_path: Path) -> None:
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000000-empty000", task_count=2),
        campaign_id="A",
    )
    sidecar = find_sidecars_by_campaign(tmp_path, "A")[0]
    assert result_dirs_for_sidecar(tmp_path, sidecar) == []


# ---------------------------------------------------------------------------
# prior
# ---------------------------------------------------------------------------


def test_prior_returns_per_iteration_reduced_metrics(tmp_path: Path) -> None:
    """Each matching sidecar contributes one reduced-metrics dict;
    iterations are ordered oldest-first."""
    # iteration 1: loss = 0.5
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000000-iter0001"),
        campaign_id="A",
    )
    _write_metrics(
        tmp_path / "results" / "20260101-000000-iter0001" / "task_0",
        {"loss": 0.5, "n_samples": 1},
    )
    time.sleep(0.01)
    # iteration 2: loss = 0.1
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000001-iter0002"),
        campaign_id="A",
    )
    _write_metrics(
        tmp_path / "results" / "20260101-000001-iter0002" / "task_0",
        {"loss": 0.1, "n_samples": 1},
    )

    history = prior(tmp_path, "A")
    assert len(history) == 2
    assert history[0]["loss"] == pytest.approx(0.5)
    assert history[1]["loss"] == pytest.approx(0.1)


def test_prior_handles_in_flight_iterations_with_empty_dict(tmp_path: Path) -> None:
    """A sidecar whose results haven't landed yet contributes ``{}``."""
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000000-pending0"),
        campaign_id="A",
    )
    history = prior(tmp_path, "A")
    assert history == [{}]


def test_prior_returns_empty_for_unknown_campaign(tmp_path: Path) -> None:
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000000-aaaaaaa"),
        campaign_id="A",
    )
    assert prior(tmp_path, "ghost") == []


def test_prior_does_not_import_tasks_py(tmp_path: Path) -> None:
    """The accessor must not import .hpc/tasks.py — closed-loop callers
    invoke prior() from inside their own tasks.py module body and an
    inner load would deadlock or recurse."""
    # Plant a sentinel tasks.py that would explode if imported.
    hpc_dir = tmp_path / ".hpc"
    hpc_dir.mkdir()
    (hpc_dir / "tasks.py").write_text("raise RuntimeError('tasks.py was imported by prior()!')\n")
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs("20260101-000000-noimport"),
        campaign_id="A",
    )
    _write_metrics(
        tmp_path / "results" / "20260101-000000-noimport" / "task_0",
        {"loss": 0.42, "n_samples": 1},
    )
    # Must not raise.
    history = prior(tmp_path, "A")
    assert history[0]["loss"] == pytest.approx(0.42)
