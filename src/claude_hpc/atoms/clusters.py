"""Cluster-config query atoms (``clusters-list``, ``clusters-describe``).

Both are pure-dispatch primitives — they read ``clusters.yaml`` and
project it to the envelope shape. Splitting them out of
``agent_cli.py`` puts the @primitive registration on a primitive-layer
function (vs an argparse adapter), which is what the C′ design
intended for the operations catalog.
"""

from __future__ import annotations

from typing import Any

from claude_hpc import errors
from claude_hpc._internal._primitive import primitive
from claude_hpc.infra.clusters import load_clusters_config


@primitive(
    name="clusters-list",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce clusters list",
)
def list_clusters() -> dict[str, Any]:
    """Return the list of configured clusters.

    Each entry: ``{name, host, scheduler}``. Reads ``clusters.yaml``
    under the package's normal config-discovery path (see
    :func:`claude_hpc.infra.clusters.load_clusters_config`).
    """
    clusters = load_clusters_config()
    return {
        "clusters": [
            {"name": name, "host": cfg.get("host"), "scheduler": cfg.get("scheduler")}
            for name, cfg in clusters.items()
        ]
    }


@primitive(
    name="clusters-describe",
    verb="query",
    side_effects=[],
    error_codes=[errors.ClusterUnknown],
    idempotent=True,
    cli="hpc-mapreduce clusters describe <name>",
)
def describe_cluster(*, name: str) -> dict[str, Any]:
    """Return the full config for a single cluster.

    Raises :class:`errors.ClusterUnknown` if the name is not present in
    ``clusters.yaml``.
    """
    clusters = load_clusters_config()
    if name not in clusters:
        raise errors.ClusterUnknown(f"unknown cluster {name!r}; run `hpc-mapreduce clusters list`")
    return {"name": name, "config": clusters[name]}
