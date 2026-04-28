"""Load cluster definitions from clusters.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``HPC_CLUSTERS_CONFIG`` env var (full path to a yaml file)
    3. ``config/clusters.yaml`` shipped inside the ``hpc_mapreduce`` package
    """
    if path is None:
        env_path = os.environ.get("HPC_CLUSTERS_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            from hpc_mapreduce import _PACKAGE_ROOT

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
