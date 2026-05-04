"""Load cluster definitions from clusters.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from claude_hpc.orchestrator.constraints import ClusterConstraints, parse_constraints


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``HPC_CLUSTERS_CONFIG`` env var (full path to a yaml file)
    3. ``config/clusters.yaml`` shipped inside the ``claude_hpc`` package
    """
    if path is None:
        env_path = os.environ.get("HPC_CLUSTERS_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            from claude_hpc import _PACKAGE_ROOT

            path = _PACKAGE_ROOT / "config" / "clusters.yaml"
    with open(path) as f:
        # yaml.safe_load returns None for an empty file; coerce to {} so
        # downstream `.get(...)` calls on the result don't AttributeError.
        result: dict[str, Any] = yaml.safe_load(f) or {}
        return result


def load_constraints(
    cluster_config: dict,
    profile_config: dict | None = None,
) -> ClusterConstraints:
    """Merge cluster-level and profile-level constraints.

    Profile constraints override cluster constraints field-by-field.
    Missing fields use cluster defaults, then ClusterConstraints defaults.
    """
    merged = {**cluster_config.get("constraints", {})}
    if profile_config is not None:
        merged.update(profile_config.get("constraints", {}))
    return parse_constraints(merged)


def get_cold_start_mem_buffer(
    cluster_config: dict[str, Any],
    *,
    default: float = 0.15,
) -> float:
    """Read the per-cluster ``cold_start_mem_buffer`` (fractional headroom).

    Returns the fraction by which a campus user's ``--mem`` ask is
    grown when no runtime prior exists for ``(profile, cluster,
    cmd_sha)`` — survival headroom against the OOM daemon for the very
    first run on a new code path. Default ``0.15`` = 15% pad. The
    smart planner takes over once ≥5 successful samples exist per
    GPU type and the buffer stops being applied (priors already
    encode the right safety margin).

    Schema validation: rejects negative values (would shrink the ask)
    but accepts ``0.0`` (legacy "kept user default" behavior).
    """
    raw = cluster_config.get("cold_start_mem_buffer", default)
    try:
        val = float(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"cold_start_mem_buffer must be a number, got {raw!r}"
        ) from e
    if val < 0:
        raise ValueError(
            f"cold_start_mem_buffer must be non-negative (it grows the ask, "
            f"never shrinks it), got {val}"
        )
    return val


def get_nfs_data_dir(cluster_config: dict[str, Any]) -> str | None:
    """Read the per-cluster ``nfs_data_dir`` if configured.

    When set, the submit-flow injects this path as ``$HPC_NFS_DATA_DIR``
    into the cluster job's env so the template preamble copies it into
    node-local SSD ($SLURM_TMPDIR/$TMPDIR) before the executor runs —
    survival against NFS throttling when N tasks read the same files
    at once. Returns ``None`` when unset (the staging block is gated
    on the env var being present, so omission is a no-op).
    """
    raw = cluster_config.get("nfs_data_dir")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"nfs_data_dir must be a non-empty string when set, got {raw!r}"
        )
    return raw
