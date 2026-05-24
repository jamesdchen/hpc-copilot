"""``clusters-list`` primitive — list every configured cluster.

Pure-dispatch: reads ``clusters.yaml`` and projects each entry to
``{name, host, scheduler}``. See
:func:`hpc_agent.infra.clusters.load_clusters_config` for the
config-discovery path.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape
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
