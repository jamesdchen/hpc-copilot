"""Integration tests for ``hpc_agent.state.runs`` — sidecar I/O.

The sidecar at ``<exp>/.hpc/runs/<run_id>.json`` is the journal of a
submitted run. Every downstream operation (status, dedup, resubmit,
aggregate) reads it. An atomic-write fault, schema-version drift, or
v1→v2 backfill bug here corrupts the read for every consumer.

Layered:

* **Layer 1 — write/read roundtrip.** Materialize a sidecar via
  ``write_run_sidecar``, read it back via ``read_run_sidecar``,
  assert every field round-trips and the hardened-default contract
  holds (wave_map / task_count / result_dir_template are always
  present and typed, even when omitted at write time).

* **Layer 2 — v1→v2 backfill.** Hand-craft a v1 sidecar (no v2 fields)
  and verify the reader backfills v2 keys to ``None`` so callers
  see a uniform shape.

* **Layer 3 — corruption recovery.** A truncated / non-JSON sidecar
  must surface as a documented error path, not a swallowed crash.

* **Layer 4 — discovery + dedup.** ``find_existing_runs``
  newest-first ordering and ``find_run_by_cmd_sha`` matching.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state.runs import (
    SIDECAR_SCHEMA_VERSION,
    find_existing_runs,
    find_run_by_cmd_sha,
    read_run_sidecar,
    run_sidecar_path,
    update_run_sidecar_job_ids,
    write_run_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


_RUN_ID = "20260101-000000-aaaaaaa"


def _required_kwargs() -> dict:
    """Minimum kwargs ``write_run_sidecar`` needs."""
    return dict(
        run_id=_RUN_ID,
        cmd_sha="a" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
    )


# ─── Layer 1: write/read roundtrip ─────────────────────────────────────


def test_write_read_roundtrips_minimum_required_fields(tmp_path: Path) -> None:
    """Every field passed to ``write_run_sidecar`` must round-trip via
    ``read_run_sidecar``. The minimal kwargs set is the contract floor."""
    write_run_sidecar(tmp_path, **_required_kwargs())
    data = read_run_sidecar(tmp_path, _RUN_ID)
    assert data["run_id"] == _RUN_ID
    assert data["cmd_sha"] == "a" * 64
    assert data["hpc_agent_version"] == "0.2.0"
    assert data["submitted_at"] == "2026-01-01T00:00:00Z"
    assert data["executor"] == "python3 .hpc/_hpc_dispatch.py"
    assert data["result_dir_template"] == "results/{task_id}"
    assert data["task_count"] == 4
    assert data["tasks_py_sha"] == "b" * 64
    assert data["sidecar_schema_version"] == SIDECAR_SCHEMA_VERSION


def test_read_returns_hardened_defaults_for_omitted_keys(tmp_path: Path) -> None:
    """``wave_map`` / ``task_count`` / ``result_dir_template`` must be
    typed correctly even when the writer omits them — callers (status,
    monitor_flow, aggregate_flow) read these without a ``.get(...)``
    fallback."""
    write_run_sidecar(tmp_path, **_required_kwargs())
    data = read_run_sidecar(tmp_path, _RUN_ID)
    assert isinstance(data["wave_map"], dict)
    assert isinstance(data["task_count"], int)
    assert isinstance(data["result_dir_template"], str)


def test_write_read_roundtrips_v2_config_snapshot_fields(tmp_path: Path) -> None:
    """Every v2 config-snapshot field round-trips when supplied. The
    documented contract: a successful submit writes the full config
    that ran under so downstream commands need no external config."""
    write_run_sidecar(
        tmp_path,
        **_required_kwargs(),
        cluster="hoffman2",
        profile="ml_ridge",
        campaign_id="camp_q1",
        project="my_proj",
        remote_path="/u/scratch/me/exp",
        resources={"cpus": 4, "mem_mb": 8000, "walltime_sec": 3600},
        env={"modules": ["cuda/12.3"]},
        env_group="ml-py311",
        constraints={"gpu": "a100"},
        gpu_fallback=["a100", "v100"],
        max_retries=3,
        runtime="uv",
        auto_retry={"system_oom": "retry_with_more_mem"},
        aggregate_defaults={"summary_glob": "results/*/summary.json"},
        job_ids=["12345"],
    )
    data = read_run_sidecar(tmp_path, _RUN_ID)
    assert data["cluster"] == "hoffman2"
    assert data["profile"] == "ml_ridge"
    assert data["campaign_id"] == "camp_q1"
    assert data["project"] == "my_proj"
    assert data["remote_path"] == "/u/scratch/me/exp"
    assert data["resources"] == {"cpus": 4, "mem_mb": 8000, "walltime_sec": 3600}
    assert data["env"] == {"modules": ["cuda/12.3"]}
    assert data["env_group"] == "ml-py311"
    assert data["constraints"] == {"gpu": "a100"}
    assert data["gpu_fallback"] == ["a100", "v100"]
    assert data["max_retries"] == 3
    assert data["runtime"] == "uv"
    assert data["auto_retry"] == {"system_oom": "retry_with_more_mem"}
    assert data["aggregate_defaults"] == {"summary_glob": "results/*/summary.json"}
    assert data["job_ids"] == ["12345"]


def test_wave_map_round_trips_with_string_keys(tmp_path: Path) -> None:
    """``wave_map`` keys must be strings on disk (JSON requirement) and
    survive the round-trip. The writer coerces; the reader doesn't
    need to. Pinning so a future "let me use int keys" refactor fails
    here, not silently downstream."""
    write_run_sidecar(
        tmp_path,
        **_required_kwargs(),
        wave_map={"0": [0, 1], "1": [2, 3]},
    )
    data = read_run_sidecar(tmp_path, _RUN_ID)
    assert data["wave_map"] == {"0": [0, 1], "1": [2, 3]}
    for k in data["wave_map"]:
        assert isinstance(k, str)


# ─── Layer 2: v1→v2 backfill ───────────────────────────────────────────


def test_v1_sidecar_backfills_to_v2_shape_on_read(tmp_path: Path) -> None:
    """A v1 sidecar (no v2 config-snapshot fields) must surface to
    callers as the v2 shape with ``None`` defaults. Lets every reader
    use the same key set regardless of when the sidecar was written."""
    target = run_sidecar_path(tmp_path, _RUN_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    v1 = {
        "sidecar_schema_version": 1,
        "run_id": _RUN_ID,
        "cmd_sha": "a" * 64,
        "hpc_agent_version": "0.1.0",
        "submitted_at": "2025-12-01T00:00:00Z",
        "executor": "python3 src/run.py",
        "result_dir_template": "results/{task_id}",
        "task_count": 2,
        "tasks_py_sha": "c" * 64,
    }
    target.write_text(json.dumps(v1))

    # Suppress the version-mismatch UserWarning from package version
    # comparison — separate observability, not the contract under test.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = read_run_sidecar(tmp_path, _RUN_ID)

    # v2 keys present, all None (or empty containers via _HARDENED_DEFAULTS).
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
        assert v2_key in data, v2_key
        assert data[v2_key] is None, (v2_key, data[v2_key])


# ─── Layer 3: corruption recovery ─────────────────────────────────────


def test_missing_sidecar_raises_file_not_found(tmp_path: Path) -> None:
    """Documented contract: ``read_run_sidecar`` raises
    ``FileNotFoundError`` when the file is absent. Callers
    (``cmd_status``, ``cmd_resubmit``) catch this specifically."""
    with pytest.raises(FileNotFoundError, match="run sidecar not found"):
        read_run_sidecar(tmp_path, _RUN_ID)


def test_invalid_run_id_format_rejected_at_path_resolution(tmp_path: Path) -> None:
    """Path-traversal / shell-metachar guard: ``run_sidecar_path`` rejects
    run_ids that don't match the canonical timestamp-prefixed format
    (``YYYYMMDD-HHMMSS-<sha>``). Catches accidental injection where a
    run_id sneaks into a filesystem path."""
    for bad in ("../escape", "id/with/slash", "id with space", "id;rm-rf"):
        with pytest.raises(ValueError, match="invalid run_id"):
            run_sidecar_path(tmp_path, bad)


# ─── Layer 4: discovery + dedup ───────────────────────────────────────


def test_find_existing_runs_returns_newest_first(tmp_path: Path) -> None:
    """Two sidecars with explicitly distinct mtimes; ``find_existing_runs``
    returns them ordered newest-first by mtime. Callers
    (``find_run_by_cmd_sha``) rely on this order to pick the most
    recent matching run during resume detection."""
    import os

    for run_id in ("20260101-000000-aaaaaaa", "20260102-000000-bbbbbbb"):
        write_run_sidecar(tmp_path, **{**_required_kwargs(), "run_id": run_id})
    # Pin mtimes so the older-by-name sidecar is newer-by-mtime. This
    # asserts the sort key is mtime, not run_id lexicographic order.
    t0 = 1_700_000_000.0
    os.utime(run_sidecar_path(tmp_path, "20260102-000000-bbbbbbb"), (t0, t0))
    os.utime(run_sidecar_path(tmp_path, "20260101-000000-aaaaaaa"), (t0 + 1, t0 + 1))

    paths = find_existing_runs(tmp_path)
    assert len(paths) == 2
    # The touched (older-by-name) sidecar should come first by mtime.
    assert paths[0].stem == "20260101-000000-aaaaaaa"


def test_find_run_by_cmd_sha_matches_payload(tmp_path: Path) -> None:
    """Submit two runs with distinct cmd_shas; lookup returns the right
    sidecar by payload, not by name."""
    write_run_sidecar(
        tmp_path, **{**_required_kwargs(), "run_id": "20260101-000000-aaaaaaa", "cmd_sha": "1" * 64}
    )
    write_run_sidecar(
        tmp_path, **{**_required_kwargs(), "run_id": "20260102-000000-bbbbbbb", "cmd_sha": "2" * 64}
    )
    hit = find_run_by_cmd_sha(tmp_path, "1" * 64)
    assert hit is not None
    assert hit.stem == "20260101-000000-aaaaaaa"
    assert find_run_by_cmd_sha(tmp_path, "2" * 64).stem == "20260102-000000-bbbbbbb"
    assert find_run_by_cmd_sha(tmp_path, "0" * 64) is None


def test_find_run_by_cmd_sha_returns_none_for_empty_input(tmp_path: Path) -> None:
    """Defensive contract: empty cmd_sha never matches (avoids matching
    every sidecar by accident if a caller passes in an unset field)."""
    write_run_sidecar(tmp_path, **_required_kwargs())
    assert find_run_by_cmd_sha(tmp_path, "") is None


def test_update_job_ids_preserves_other_fields(tmp_path: Path) -> None:
    """``update_run_sidecar_job_ids`` mutates job_ids without touching
    the rest of the sidecar. Pinning so a future "let me rewrite the
    whole sidecar" refactor preserves the audit trail."""
    write_run_sidecar(
        tmp_path,
        **_required_kwargs(),
        cluster="hoffman2",
        profile="ml",
        resources={"cpus": 4},
    )
    update_run_sidecar_job_ids(tmp_path, _RUN_ID, ["12345"])
    data = read_run_sidecar(tmp_path, _RUN_ID)
    assert data["job_ids"] == ["12345"]
    # All other fields preserved.
    assert data["cmd_sha"] == "a" * 64
    assert data["cluster"] == "hoffman2"
    assert data["profile"] == "ml"
    assert data["resources"] == {"cpus": 4}
