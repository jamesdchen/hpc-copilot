"""Cluster-config query atoms (``clusters-list``, ``clusters-describe``).

Both are pure-dispatch primitives — they read ``clusters.yaml`` and
project it to the envelope shape. Splitting them out of
``agent_cli.py`` puts the @primitive registration on a primitive-layer
function (vs an argparse adapter), which is what the C′ design
intended for the operations catalog.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import load_clusters_config


@primitive(
    name="clusters-list",
    verb="query",
    side_effects=[],
    error_codes=[errors.ConfigInvalid],
    idempotent=True,
    cli=CliShape(help="List all clusters.", group="clusters"),
    agent_facing=True,
)
def list_clusters() -> dict[str, Any]:
    """Return the list of configured clusters.

    Each entry: ``{name, host, scheduler}``. Reads ``clusters.yaml``
    under the package's normal config-discovery path (see
    :func:`hpc_agent.infra.clusters.load_clusters_config`).
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
    error_codes=[errors.ClusterUnknown, errors.ConfigInvalid],
    idempotent=True,
    cli=CliShape(
        help="Print one cluster's config.",
        group="clusters",
        args=(
            CliArg("name", type=str, help="Cluster name."),
            CliArg(
                "--strict",
                action="store_true",
                help=(
                    "Surface yaml keys not recognized by ClusterConfig under "
                    "data.unknown_keys. Useful for catching typos that the "
                    "default extra='ignore' validation would silently drop."
                ),
            ),
        ),
    ),
    agent_facing=True,
)
def describe_cluster(*, name: str, strict: bool = False) -> dict[str, Any]:
    """Return the full config for a single cluster.

    Raises :class:`errors.ClusterUnknown` if the name is not present in
    ``clusters.yaml``.

    When *strict* is ``True``, surface every yaml key that
    ``ClusterConfig`` does not recognize under ``unknown_keys``.
    ``ClusterConfig`` itself stays ``extra="ignore"`` for back-compat
    (flipping the default would break every existing user's
    ``clusters.yaml`` that carries a typo or a stale field), so the
    strict pass is opt-in.
    """
    from hpc_agent.infra.clusters import ClusterConfig

    clusters = load_clusters_config()
    if name not in clusters:
        raise errors.ClusterUnknown(f"unknown cluster {name!r}; run `hpc-agent clusters list`")
    cfg = clusters[name]
    out: dict[str, Any] = {"name": name, "config": cfg}
    if strict:
        allowed = set(ClusterConfig.model_fields.keys())
        unknown = sorted(k for k in cfg if k not in allowed) if isinstance(cfg, dict) else []
        out["unknown_keys"] = unknown
    return out
