"""Load, validate, and build environment from hpc.yaml experiment manifests."""

from __future__ import annotations

__all__ = [
    "load_manifest",
    "manifest_exists",
    "validate_manifest",
    "build_manifest_env",
    "resolve_template",
    "normalize_profile",
    "resolve_effective_config",
]

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from hpc.grid import total_tasks

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``config/clusters.yaml`` relative to the package root
    """
    if path is None:
        path = _PACKAGE_ROOT / "config" / "clusters.yaml"
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def detect_project_type(path: Path | None = None) -> str:
    """Return ``"manifest"`` if ``hpc.yaml`` exists, else ``"none"``."""
    base = path or Path.cwd()
    if (base / "hpc.yaml").exists():
        return "manifest"
    return "none"


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Load ``hpc.yaml`` from *path* (default: cwd)."""
    if path is None:
        path = Path.cwd() / "hpc.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def manifest_exists(path: Path | None = None) -> bool:
    """Return True if ``hpc.yaml`` exists at *path* (default: cwd)."""
    if path is None:
        path = Path.cwd() / "hpc.yaml"
    return path.exists()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_stage(stage: dict[str, Any], name: str, prefix: str) -> list[str]:
    """Validate a single stage (or single-stage profile). Returns error strings."""
    errors: list[str] = []

    if "run" not in stage:
        errors.append(f"{prefix}'{name}' missing 'run'")

    resources = stage.get("resources")
    if resources is None:
        errors.append(f"{prefix}'{name}' missing 'resources'")
    elif isinstance(resources, dict):
        for key in ("mem", "walltime"):
            if key not in resources:
                errors.append(f"{prefix}'{name}.resources' missing '{key}'")

    grid = stage.get("grid")
    if grid is not None:
        if not isinstance(grid, dict) or not grid:
            errors.append(f"{prefix}'{name}.grid' must be a non-empty dict")
        elif not all(isinstance(v, list) for v in grid.values()):
            errors.append(f"{prefix}'{name}.grid' values must be lists")

    chunking = stage.get("chunking")
    if chunking is not None:
        total = chunking.get("total")
        if not isinstance(total, int) or total < 1:
            errors.append(f"{prefix}'{name}.chunking.total' must be a positive int")

    env = stage.get("env")
    if env is not None and not isinstance(env, dict):
        errors.append(f"{prefix}'{name}.env' must be a dict")

    return errors


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Validate a parsed manifest dict. Return list of error strings (empty = valid)."""
    errors: list[str] = []

    # Top-level required keys (always needed)
    top_required = {"project", "cluster", "remote_path"}

    profiles = manifest.get("profiles")

    if profiles is not None:
        # Profiles mode — run/grid/resources live inside each profile
        missing = top_required - manifest.keys()
        if missing:
            errors.append(f"Missing required top-level keys: {sorted(missing)}")

        if not isinstance(profiles, dict) or not profiles:
            errors.append("'profiles' must be a non-empty dict")
        else:
            for prof_name, prof_cfg in profiles.items():
                if not isinstance(prof_cfg, dict):
                    errors.append(f"Profile '{prof_name}' must be a dict")
                    continue

                stages = prof_cfg.get("stages")
                if stages is not None:
                    # Multi-stage profile
                    if not isinstance(stages, dict) or not stages:
                        errors.append(
                            f"Profile '{prof_name}.stages' must be a non-empty dict"
                        )
                    else:
                        for stg_name, stg_cfg in stages.items():
                            if not isinstance(stg_cfg, dict):
                                errors.append(
                                    f"Stage '{prof_name}.{stg_name}' must be a dict"
                                )
                                continue
                            errors.extend(
                                _validate_stage(
                                    stg_cfg,
                                    stg_name,
                                    f"profiles.{prof_name}.stages.",
                                )
                            )
                else:
                    # Single-stage profile (run/grid/resources at profile level)
                    errors.extend(
                        _validate_stage(prof_cfg, prof_name, "profiles.")
                    )
    else:
        # Single-profile shorthand — run/grid/resources at top level
        required = top_required | {"run", "grid", "resources"}
        missing = required - manifest.keys()
        if missing:
            errors.append(f"Missing required top-level keys: {sorted(missing)}")

        # Validate as a single stage
        errors.extend(_validate_stage(manifest, "top-level", ""))

    return errors


# ---------------------------------------------------------------------------
# Profile normalization
# ---------------------------------------------------------------------------


def normalize_profile(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize a profile to a stages dict.

    If the profile has a ``stages`` key, return it as-is.
    If the profile has ``run`` at the top level, wrap it in a single
    ``{"default": ...}`` stage so all downstream code can work uniformly.
    """
    if "stages" in profile:
        return profile["stages"]

    # Single-stage profile — extract stage-level keys
    stage_keys = {
        "run", "grid", "chunking", "env", "env_group", "resources",
        "results", "gpu_fallback", "max_retries",
    }
    stage = {k: v for k, v in profile.items() if k in stage_keys}
    return {"default": stage}


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def resolve_effective_config(
    manifest: dict[str, Any],
    profile_name: str | None = None,
    stage_name: str | None = None,
) -> dict[str, Any]:
    """Resolve the effective stage config from a manifest.

    When *profile_name* is given, looks up that profile and normalizes it.
    When *stage_name* is also given, returns that specific stage.
    Otherwise returns the manifest itself (single-profile shorthand).
    """
    if profile_name is not None:
        prof = manifest["profiles"][profile_name]
        stages = normalize_profile(prof)
        return stages[stage_name or "default"]
    return manifest


# ---------------------------------------------------------------------------
# Environment builder
# ---------------------------------------------------------------------------


def build_manifest_env(
    manifest: dict[str, Any],
    profile_name: str | None = None,
    stage_name: str | None = None,
) -> dict[str, str]:
    """Build template env vars from a manifest for job submission.

    When *profile_name* is given, uses that profile's config.
    When *stage_name* is given (for multi-stage profiles), uses that stage's config.
    """
    clusters = load_clusters_config()
    cluster_name = manifest["cluster"]
    cluster = clusters[cluster_name]

    effective = resolve_effective_config(manifest, profile_name, stage_name)

    env_cfg = effective.get("env", {})

    # Check for cluster_envs override
    env_group = effective.get("env_group")
    cluster_envs = manifest.get("cluster_envs", {})
    if env_group and cluster_name in cluster_envs:
        cluster_override = cluster_envs[cluster_name].get(env_group)
        if cluster_override:
            env_cfg = cluster_override

    chunks = 1
    chunking = effective.get("chunking")
    if chunking:
        chunks = chunking.get("total", 1)

    grid = effective.get("grid", {})

    result: dict[str, str] = {
        "EXECUTOR": "python3 _hpc_dispatch.py",
        "HPC_MANIFEST": "_hpc_dispatch.json",
        "REPO_DIR": manifest["remote_path"],
        "MODULES": env_cfg.get("modules", ""),
        "TOTAL_CHUNKS": str(total_tasks(grid, chunks) if grid else chunks),
    }

    conda_env = env_cfg.get("conda_env")
    if conda_env:
        result["CONDA_SOURCE"] = cluster["conda_source"]
        result["CONDA_ENV"] = conda_env

    return result


def resolve_template(
    manifest: dict[str, Any],
    profile_name: str | None = None,
    stage_name: str | None = None,
) -> str:
    """Determine job template name from manifest resources."""
    effective = resolve_effective_config(manifest, profile_name, stage_name)

    if "gpus" in effective.get("resources", {}):
        return "gpu_array"
    return "cpu_array"
