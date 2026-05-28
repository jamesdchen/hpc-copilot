"""Tests for run sidecar v2 schema and v1→v2 backfill."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state.journal import upsert_run
from hpc_agent.state.runs import (
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
        hpc_agent_version="0.2.0",
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
        "hpc_agent_version": "0.1.0",
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
                "hpc_agent_version": "0.0.1",
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


class TestVersionMismatchWarning:
    """A10: read_run_sidecar warns once per (run_id, sidecar_version) when
    the sidecar's ``hpc_agent_version`` differs from the running
    package's ``__version__``. Reads always succeed regardless of the
    warning.
    """

    def test_warning_fires_on_version_mismatch(self, tmp_path: Path) -> None:
        import warnings as _warnings

        from hpc_agent.state import runs as _runs_mod

        # Reset module-level dedup set so this test is hermetic regardless
        # of test ordering.
        _runs_mod._warned_version_mismatch.clear()

        run_id = "20260101-000000-deadbee"
        target = run_sidecar_path(tmp_path, run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "sidecar_schema_version": 2,
                    "run_id": run_id,
                    "cmd_sha": "0" * 64,
                    "hpc_agent_version": "9.9.9-from-the-future",
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "executor": "python3 src/run.py",
                    "result_dir_template": "results/{seed}",
                    "task_count": 0,
                    "tasks_py_sha": "1" * 64,
                }
            )
        )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            data = read_run_sidecar(tmp_path, run_id)
        # Read succeeds.
        assert data["run_id"] == run_id
        # And exactly one warning fires.
        msgs = [str(w.message) for w in caught]
        assert any("9.9.9-from-the-future" in m for m in msgs), (
            "expected version-mismatch warning, got: " + repr(msgs)
        )

    def test_warning_dedupes_per_run_and_version(self, tmp_path: Path) -> None:
        import warnings as _warnings

        from hpc_agent.state import runs as _runs_mod

        _runs_mod._warned_version_mismatch.clear()
        run_id = "20260101-000000-deadbee"
        target = run_sidecar_path(tmp_path, run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "sidecar_schema_version": 2,
                    "run_id": run_id,
                    "cmd_sha": "0" * 64,
                    "hpc_agent_version": "9.9.9-from-the-future",
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "executor": "python3 src/run.py",
                    "result_dir_template": "results/{seed}",
                    "task_count": 0,
                    "tasks_py_sha": "1" * 64,
                }
            )
        )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            for _ in range(5):
                read_run_sidecar(tmp_path, run_id)
        version_warnings = [w for w in caught if "9.9.9-from-the-future" in str(w.message)]
        assert len(version_warnings) == 1, (
            f"expected exactly one warning across 5 reads; got {len(version_warnings)}"
        )

    def test_no_warning_when_versions_match(self, tmp_path: Path) -> None:
        import warnings as _warnings

        from hpc_agent import __version__ as pkg_version
        from hpc_agent.state import runs as _runs_mod

        _runs_mod._warned_version_mismatch.clear()
        run_id = "20260101-000000-cafebab"
        target = run_sidecar_path(tmp_path, run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "sidecar_schema_version": 2,
                    "run_id": run_id,
                    "cmd_sha": "0" * 64,
                    "hpc_agent_version": pkg_version,
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "executor": "python3 src/run.py",
                    "result_dir_template": "results/{seed}",
                    "task_count": 0,
                    "tasks_py_sha": "1" * 64,
                }
            )
        )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            read_run_sidecar(tmp_path, run_id)
        assert not any(
            "hpc-agent" in str(w.message) and "but reader is" in str(w.message) for w in caught
        ), "matching versions should not warn"


# ---------------------------------------------------------------------------
# Auto-derived wave_map from axes.yaml
# ---------------------------------------------------------------------------


def test_auto_derive_wave_map_from_axes_yaml(tmp_path: Path) -> None:
    """Caller omits wave_map; axes.yaml present → sidecar carries derived map."""
    from hpc_agent.state.axes import write_axes

    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 2}, {"name": "window", "size": 3}],
        homogeneous_axes=["window"],
    )
    kwargs = _common_required_kwargs()
    kwargs["task_count"] = 6  # 2 * 3
    write_run_sidecar(tmp_path, **kwargs)
    out = read_run_sidecar(tmp_path, kwargs["run_id"])
    # window picked → 2 waves of 3 task_ids each.
    assert out["wave_map"] == {"0": [0, 1, 2], "1": [3, 4, 5]}


def test_auto_derive_skipped_without_axes_yaml(tmp_path: Path) -> None:
    """No axes.yaml → wave_map remains absent (read backfills to {})."""
    write_run_sidecar(tmp_path, **_common_required_kwargs())
    out = read_run_sidecar(tmp_path, "20260101-000000-deadbee")
    assert out["wave_map"] == {}


def test_auto_derive_skipped_when_axes_yaml_lacks_enumeration(tmp_path: Path) -> None:
    """axes.yaml present but no axes list → no derivation, no warning."""
    import warnings as _warnings

    from hpc_agent.state.axes import write_axes

    write_axes(tmp_path, homogeneous_axes=["window"])
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        write_run_sidecar(tmp_path, **_common_required_kwargs())
    assert not any("axes.yaml product" in str(w.message) for w in caught)
    out = read_run_sidecar(tmp_path, "20260101-000000-deadbee")
    assert out["wave_map"] == {}


def test_auto_derive_warns_on_axes_product_mismatch(tmp_path: Path) -> None:
    """axes-product != task_count → UserWarning, no derived wave_map."""
    import warnings as _warnings

    from hpc_agent.state.axes import write_axes

    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 2}, {"name": "window", "size": 3}],
    )
    kwargs = _common_required_kwargs()
    kwargs["task_count"] = 7  # mismatch — axes say 6
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        write_run_sidecar(tmp_path, **kwargs)
    assert any(
        "axes.yaml product" in str(w.message) and "task_count" in str(w.message) for w in caught
    ), "mismatch should emit a UserWarning"
    out = read_run_sidecar(tmp_path, kwargs["run_id"])
    assert out["wave_map"] == {}  # backfill default


def test_explicit_wave_map_skips_auto_derive(tmp_path: Path) -> None:
    """Caller-supplied wave_map is preserved verbatim; no axes.yaml lookup."""
    from hpc_agent.state.axes import write_axes

    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 2}, {"name": "window", "size": 3}],
    )
    kwargs = _common_required_kwargs()
    kwargs["task_count"] = 6
    explicit = {"0": [0, 1, 2, 3, 4, 5]}  # different from any derivation
    write_run_sidecar(tmp_path, wave_map=explicit, **kwargs)
    out = read_run_sidecar(tmp_path, kwargs["run_id"])
    assert out["wave_map"] == explicit


# ---------------------------------------------------------------------------
# Orphan sidecar detection + prune primitive
# ---------------------------------------------------------------------------


@pytest.fixture
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def _seed_journal(experiment: Path, run_id: str, *, job_ids: list[str]) -> None:
    """Write a journal RunRecord matching *run_id* with the given job_ids."""
    from hpc_agent.state.run_record import RunRecord

    record = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@h",
        remote_path="/x",
        job_name="j",
        job_ids=job_ids,
        total_tasks=1,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    upsert_run(experiment, record)


def test_is_orphan_when_no_journal_record(_journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.state.runs import is_orphan_sidecar

    write_run_sidecar(tmp_path, **_common_required_kwargs())
    assert is_orphan_sidecar(tmp_path, "20260101-000000-deadbee") is True


def test_is_not_orphan_when_journal_has_job_ids(_journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.state.runs import is_orphan_sidecar

    kwargs = _common_required_kwargs()
    write_run_sidecar(tmp_path, **kwargs)
    _seed_journal(tmp_path, kwargs["run_id"], job_ids=["12345"])
    assert is_orphan_sidecar(tmp_path, kwargs["run_id"]) is False


def test_is_orphan_when_journal_has_empty_job_ids(_journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.state.runs import is_orphan_sidecar

    kwargs = _common_required_kwargs()
    write_run_sidecar(tmp_path, **kwargs)
    _seed_journal(tmp_path, kwargs["run_id"], job_ids=[])
    assert is_orphan_sidecar(tmp_path, kwargs["run_id"]) is True


def test_prune_orphan_sidecars_removes_only_orphans(_journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.state.runs import prune_orphan_sidecars, run_sidecar_path

    real_kwargs = _common_required_kwargs(run_id="20260101-000000-real0001")
    orphan_kwargs = _common_required_kwargs(run_id="20260101-000001-orphan02")
    write_run_sidecar(tmp_path, **real_kwargs)
    write_run_sidecar(tmp_path, **orphan_kwargs)
    _seed_journal(tmp_path, real_kwargs["run_id"], job_ids=["999"])

    # min_age_seconds=0 — this isolated test has no concurrent submit to
    # race the prune; the production default (300s) protects ad-hoc CLI
    # invocations from deleting a sidecar an in-flight submit is about
    # to finalize.
    deleted = prune_orphan_sidecars(tmp_path, min_age_seconds=0)
    assert deleted == [orphan_kwargs["run_id"]]
    assert run_sidecar_path(tmp_path, real_kwargs["run_id"]).is_file()
    assert not run_sidecar_path(tmp_path, orphan_kwargs["run_id"]).is_file()


def test_prune_orphan_sidecars_idempotent(_journal_home: Path, tmp_path: Path) -> None:
    from hpc_agent.state.runs import prune_orphan_sidecars

    write_run_sidecar(tmp_path, **_common_required_kwargs())
    # min_age_seconds=0 — see test_prune_orphan_sidecars_removes_only_orphans.
    first = prune_orphan_sidecars(tmp_path, min_age_seconds=0)
    second = prune_orphan_sidecars(tmp_path, min_age_seconds=0)
    assert len(first) == 1
    assert second == []


def test_prune_orphan_sidecars_skips_excluded_runs(_journal_home: Path, tmp_path: Path) -> None:
    """The run currently being submitted (and its canary) must survive
    the prune. ``submit_flow_batch`` writes the jobless Step-6d sidecar
    before calling prune inside the lock, so without ``exclude`` it looks
    exactly like a prior batch's orphan and gets deleted out from under
    the in-flight submit (regression for the 'sidecar not found' crash)."""
    from hpc_agent.state.runs import prune_orphan_sidecars, run_sidecar_path

    current = _common_required_kwargs(run_id="20260101-000000-current01")
    canary = _common_required_kwargs(run_id="20260101-000000-current01-canary")
    stale = _common_required_kwargs(run_id="20260101-000001-stale0002")
    write_run_sidecar(tmp_path, **current)
    write_run_sidecar(tmp_path, **canary)
    write_run_sidecar(tmp_path, **stale)

    deleted = prune_orphan_sidecars(
        tmp_path,
        min_age_seconds=0,
        exclude={current["run_id"], canary["run_id"]},
    )

    assert deleted == [stale["run_id"]]
    assert run_sidecar_path(tmp_path, current["run_id"]).is_file()
    assert run_sidecar_path(tmp_path, canary["run_id"]).is_file()
    assert not run_sidecar_path(tmp_path, stale["run_id"]).is_file()


def test_find_run_by_cmd_sha_default_preserves_journal_wipe_recovery(
    _journal_home: Path, tmp_path: Path
) -> None:
    """Default behaviour: a sidecar without a journal record IS findable
    so :func:`runner.submit_and_record` can reconstruct the journal."""
    from hpc_agent.state.runs import find_run_by_cmd_sha

    cmd_sha = "f" * 64
    kwargs = _common_required_kwargs()
    kwargs["cmd_sha"] = cmd_sha
    write_run_sidecar(tmp_path, **kwargs)
    # No journal record — but default match still hits, preserving the
    # journal-wipe recovery contract.
    found = find_run_by_cmd_sha(tmp_path, cmd_sha)
    assert found is not None and found.stem == kwargs["run_id"]


def test_find_run_by_cmd_sha_with_skip_orphans_drops_half_baked(
    _journal_home: Path, tmp_path: Path
) -> None:
    """Opt-in flag for callers that have already pruned the failed batch."""
    from hpc_agent.state.runs import find_run_by_cmd_sha

    cmd_sha = "e" * 64
    kwargs = _common_required_kwargs()
    kwargs["cmd_sha"] = cmd_sha
    write_run_sidecar(tmp_path, **kwargs)
    assert find_run_by_cmd_sha(tmp_path, cmd_sha, skip_orphans=True) is None


# ---------------------------------------------------------------------------
# job_ids on the sidecar — pending vs committed signal
# ---------------------------------------------------------------------------


def test_sidecar_with_job_ids_is_not_orphan_even_without_journal(
    _journal_home: Path, tmp_path: Path
) -> None:
    """Journal-wipe recovery: a sidecar that finalize_run_sidecar_job_ids
    stamped is the canonical 'job ran on the cluster' signal — even if
    the journal at ~/.claude/hpc/<repo_hash>/ has since been wiped."""
    from hpc_agent.state.runs import is_orphan_sidecar, write_run_sidecar

    write_run_sidecar(tmp_path, **_common_required_kwargs(), job_ids=["12345"])
    assert is_orphan_sidecar(tmp_path, "20260101-000000-deadbee") is False


def test_sidecar_without_job_ids_or_journal_is_orphan(_journal_home: Path, tmp_path: Path) -> None:
    """The half-baked case: Step 6d wrote the sidecar but qsub never ran."""
    from hpc_agent.state.runs import is_orphan_sidecar, write_run_sidecar

    write_run_sidecar(tmp_path, **_common_required_kwargs())
    assert is_orphan_sidecar(tmp_path, "20260101-000000-deadbee") is True


def test_update_sidecar_job_ids_atomically_stamps_existing_sidecar(
    _journal_home: Path, tmp_path: Path
) -> None:
    """Post-qsub finalize: load + set + atomic rewrite, preserving v2 fields."""
    from hpc_agent.state.runs import (
        is_orphan_sidecar,
        read_run_sidecar,
        update_run_sidecar_job_ids,
        write_run_sidecar,
    )

    # Write a "pending" sidecar with rich v2 fields (mirrors the slash
    # command Step 6d call shape).
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs(),
        cluster="hoffman2",
        resources={"cpus": 8, "mem": "64G"},
    )
    rid = "20260101-000000-deadbee"
    assert is_orphan_sidecar(tmp_path, rid) is True

    # Finalize it.
    update_run_sidecar_job_ids(tmp_path, rid, ["job_42", "job_43"])

    data = read_run_sidecar(tmp_path, rid)
    assert data["job_ids"] == ["job_42", "job_43"]
    # v2 fields preserved verbatim.
    assert data["cluster"] == "hoffman2"
    assert data["resources"] == {"cpus": 8, "mem": "64G"}
    assert is_orphan_sidecar(tmp_path, rid) is False


def test_update_sidecar_job_ids_raises_when_sidecar_missing(
    _journal_home: Path, tmp_path: Path
) -> None:
    """Caller-side bug: finalize before write. Surface, don't synthesize."""
    from hpc_agent.state.runs import update_run_sidecar_job_ids

    with pytest.raises(FileNotFoundError):
        update_run_sidecar_job_ids(tmp_path, "20260101-000000-nope0000", ["12345"])
