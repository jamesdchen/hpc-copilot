"""Tests for ``slash_commands.runner.build_job_env``.

The helper threads the manifest's ``runtime`` field into the qsub /
sbatch env so the template's ``uv sync`` preamble fires when
``runtime: "uv"`` is configured. Pure function — no SSH, no I/O.
"""

from __future__ import annotations

from slash_commands.runner import build_job_env

_BASE_ENV = {
    "EXECUTOR": "python3 _hpc_dispatch.py",
    "HPC_MANIFEST": "_hpc_dispatch.json",
    "REPO_DIR": "/u/scratch/exp",
}


def test_no_runtime_returns_copy() -> None:
    """Manifest without a ``runtime`` field → identical-key copy of base."""
    manifest: dict = {"schema_version": 2, "total_tasks": 4}
    out = build_job_env(manifest, _BASE_ENV)
    assert out == _BASE_ENV


def test_uv_adds_hpc_runtime() -> None:
    """``runtime: "uv"`` augments base_env with ``HPC_RUNTIME=uv``."""
    manifest: dict = {"schema_version": 2, "runtime": "uv"}
    out = build_job_env(manifest, _BASE_ENV)
    assert out["HPC_RUNTIME"] == "uv"
    for k, v in _BASE_ENV.items():
        assert out[k] == v


def test_does_not_mutate_inputs() -> None:
    """Defensive: callers may pass shared dicts."""
    base = dict(_BASE_ENV)
    manifest: dict = {"runtime": "uv"}
    _ = build_job_env(manifest, base)
    assert base == _BASE_ENV
    assert manifest == {"runtime": "uv"}


def test_unknown_runtime_no_op() -> None:
    """Future-proof: typo or unknown profile doesn't accidentally set HPC_RUNTIME."""
    manifest: dict = {"runtime": "pixi"}
    out = build_job_env(manifest, _BASE_ENV)
    assert "HPC_RUNTIME" not in out
    assert out == _BASE_ENV


def test_returns_new_dict_when_uv() -> None:
    """Identity check: even when augmented, the returned dict is a copy."""
    manifest: dict = {"runtime": "uv"}
    out = build_job_env(manifest, _BASE_ENV)
    assert out is not _BASE_ENV
