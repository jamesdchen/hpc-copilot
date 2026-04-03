"""Load cluster definitions from clusters.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pathlib import Path


def load_clusters_config(path: Path | None = None) -> dict[str, Any]:
    """Load cluster definitions from clusters.yaml.

    Searches (in order):
    1. Explicit *path* argument
    2. ``config/clusters.yaml`` relative to the package root
    """
    if path is None:
        from hpc_mapreduce import _PACKAGE_ROOT

        path = _PACKAGE_ROOT / "config" / "clusters.yaml"
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result
