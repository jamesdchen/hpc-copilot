"""Tests for run sidecar v2 schema and v1→v2 backfill."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_mapreduce.job.runs import (
    SIDECAR_SCHEMA_VERSION,
    read_run_sidecar,
    run_sidecar_path,
    write_run_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


def _common_required_kwargs(run_id: str = "20260101-000000-deadbee") -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        claude_hpc_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
    )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_schema_version_is_2() -> None:
    assert SIDECAR_SCHEMA_VERSION == 2


# ---------------------------------------------------------------------------
# v2 write/read round-trip
# ---------------------------------------------------------------------------


def test_v2_write_then_read_roundtrips_all_config_fields(tmp_path: Path) -> None:
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs(),
        cluster="hoffman2",
        profile="ml_ridge",
        campaign_id="ml_ridge_q1",
        project="ml-ridge",
        remote_path="/u/scratch/u/me/ml_ridge",
        resources={"cpus": 4, "mem": "16G", "walltime": "02:00:00"},
        env={"modules": "python/3.11.9", "conda_env": "ml"},
        env_group="default",
        constraints={"max_array_size": 500},
        gpu_fallback=["a100", "h100"],
        max_retries=3,
        runtime="uv",
        auto_retry={"oom": {"max_attempts": 2}},
        aggregate_defaults={"require_outputs": "results/{seed}/metrics.json"},
    )
    data = read_run_sidecar(tmp_path, _common_required_kwargs()["run_id"])
    assert data["sidecar_schema_version"] == 2
    assert data["cluster"] == "hoffman2"
    assert data["profile"] == "ml_ridge"
    assert data["campaign_id"] == "ml_ridge_q1"
    assert data["project"] == "ml-ridge"
    assert data["remote_path"] == "/u/scratch/u/me/ml_ridge"
    assert data["resources"] == {"cpus": 4, "mem": "16G", "walltime": "02:00:00"}
    assert data["env"] == {"modules": "python/3.11.9", "conda_env": "ml"}
    assert data["env_group"] == "default"
    assert data["constraints"] == {"max_array_size": 500}
    assert data["gpu_fallback"] == ["a100", "h100"]
    assert data["max_retries"] == 3
    assert data["runtime"] == "uv"
    assert data["auto_retry"] == {"oom": {"max_attempts": 2}}
    assert data["aggregate_defaults"] == {"require_outputs": "results/{seed}/metrics.json"}


def test_v2_write_omits_none_keys_to_keep_sidecar_compact(tmp_path: Path) -> None:
    """Optional v2 kwargs left as ``None`` must NOT appear in the on-disk JSON."""
    write_run_sidecar(tmp_path, **_common_required_kwargs(), cluster="hoffman2")
    raw = json.loads(run_sidecar_path(tmp_path, _common_required_kwargs()["run_id"]).read_text())
    assert "cluster" in raw
    # All other v2 fields were left as None and must not be persisted.
    for omitted in (
        "profile",
        "campaign_id",
        "project",
        "remote_path",
        "resources",
        "env",
        "env_group",
        "constraints",
        "gpu_fallback",
        "max_retries",
        "runtime",
        "auto_retry",
        "aggregate_defaults",
    ):
        assert omitted not in raw, f"{omitted!r} should be omitted when None"


# ---------------------------------------------------------------------------
# v1 → v2 backfill on read
# ---------------------------------------------------------------------------


def test_v1_sidecar_reads_with_backfilled_v2_fields(tmp_path: Path) -> None:
    """Old sidecars on disk (schema_version=1) must load and have v2 keys
    backfilled to ``None`` so callers can rely on the v2 shape."""
    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True)
    run_id = "20240101-000000-legacy00"
    v1_payload = {
        "sidecar_schema_version": 1,
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "claude_hpc_version": "0.1.0",
        "submitted_at": "2024-01-01T00:00:00Z",
        "executor": "python3 old.py",
        "result_dir_template": "out/{seed}",
        "task_count": 1,
        "tasks_py_sha": "b" * 64,
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(v1_payload))
    data = read_run_sidecar(tmp_path, run_id)
    # Original v1 fields preserved.
    assert data["sidecar_schema_version"] == 1
    assert data["executor"] == "python3 old.py"
    # v2 keys backfilled to None.
    for v2_key in (
        "cluster",
        "profile",
        "campaign_id",
        "project",
        "remote_path",
        "resources",
        "env",
        "env_group",
        "constraints",
        "gpu_fallback",
        "max_retries",
        "runtime",
        "auto_retry",
        "aggregate_defaults",
    ):
        assert v2_key in data, f"v2 key {v2_key!r} must be backfilled when reading v1"
        assert data[v2_key] is None


# ---------------------------------------------------------------------------
# Existing kwargs still work (back-compat for callers that don't pass v2)
# ---------------------------------------------------------------------------


def test_write_without_any_v2_kwargs_still_works(tmp_path: Path) -> None:
    """Callers that only pass v1-era kwargs must continue to function."""
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs(),
        wave_map={"0": [0, 1], "1": [2, 3]},
        extra={"submitted_by": "alice"},
    )
    data = read_run_sidecar(tmp_path, _common_required_kwargs()["run_id"])
    assert data["sidecar_schema_version"] == 2
    assert data["wave_map"] == {"0": [0, 1], "1": [2, 3]}
    assert data["extra"] == {"submitted_by": "alice"}
    # All v2 config fields backfilled to None since none were supplied.
    assert data["cluster"] is None
    assert data["resources"] is None


def test_read_missing_sidecar_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_run_sidecar(tmp_path, "20260101-000000-nope0000")


# ---------------------------------------------------------------------------
# Hardened return shape — wave_map / task_count / result_dir_template
# guaranteed present (regression: monitor_flow / aggregate_flow / status /
# history all used to read these via raw json.loads from the wrong dir).
# ---------------------------------------------------------------------------


def test_wave_map_defaults_to_empty_dict_when_omitted(tmp_path: Path) -> None:
    # Write without wave_map.
    write_run_sidecar(tmp_path, **_common_required_kwargs())
    data = read_run_sidecar(tmp_path, _common_required_kwargs()["run_id"])
    assert "wave_map" in data
    assert data["wave_map"] == {}
    assert isinstance(data["wave_map"], dict)


def test_wave_map_preserved_when_present(tmp_path: Path) -> None:
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs(),
        wave_map={"0": [0, 1, 2], "1": [3]},
    )
    data = read_run_sidecar(tmp_path, _common_required_kwargs()["run_id"])
    assert data["wave_map"] == {"0": [0, 1, 2], "1": [3]}


def test_task_count_present_and_int(tmp_path: Path) -> None:
    write_run_sidecar(tmp_path, **_common_required_kwargs())
    data = read_run_sidecar(tmp_path, _common_required_kwargs()["run_id"])
    assert "task_count" in data
    assert isinstance(data["task_count"], int)
    assert data["task_count"] == 4


def test_result_dir_template_present_and_str(tmp_path: Path) -> None:
    write_run_sidecar(tmp_path, **_common_required_kwargs())
    data = read_run_sidecar(tmp_path, _common_required_kwargs()["run_id"])
    assert "result_dir_template" in data
    assert isinstance(data["result_dir_template"], str)
    assert data["result_dir_template"] == "results/{seed}"


def test_v1_sidecar_without_wave_map_still_yields_empty_dict(tmp_path: Path) -> None:
    """Hand-craft a v1 sidecar lacking wave_map; the hardened reader must
    still produce wave_map={} so downstream code (auto_combine_waves /
    ensure_all_combined) can rely on the shape."""
    run_id = "20260101-000000-deadbee"
    target = run_sidecar_path(tmp_path, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "sidecar_schema_version": 1,
                "run_id": run_id,
                "cmd_sha": "0" * 64,
                "claude_hpc_version": "0.0.1",
                "submitted_at": "2026-01-01T00:00:00Z",
                "executor": "python3 old.py",
                "result_dir_template": "results/{seed}",
                "task_count": 0,
                "tasks_py_sha": "1" * 64,
                # NOTE: no wave_map key
            }
        )
    )
    data = read_run_sidecar(tmp_path, run_id)
    assert data["wave_map"] == {}
    assert data["task_count"] == 0
    assert data["result_dir_template"] == "results/{seed}"
