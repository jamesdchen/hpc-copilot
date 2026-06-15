"""``clusters-describe`` primitive — print one cluster's config.

Pure-dispatch: reads ``clusters.yaml``, returns the resolved
``ClusterConfig`` dict, and (when ``--strict``) reports yaml keys not
recognized by ``ClusterConfig`` under ``data.unknown_keys``.
``ClusterConfig`` itself stays ``extra="ignore"`` for back-compat (a
default flip would break every user with a stale field), so the
strict pass is opt-in.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import load_clusters_config


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
    if not isinstance(cfg, dict):
        # The output schema types `config` as an object. A clusters.yaml entry
        # that isn't a mapping (e.g. a bare scalar or list) would otherwise be
        # emitted verbatim and fail output validation as an opaque `internal`
        # error — surface it as a clear config error instead.
        raise errors.ConfigInvalid(
            f"cluster {name!r} entry in clusters.yaml must be a mapping, got {type(cfg).__name__}"
        )
    out: dict[str, Any] = {"name": name, "config": cfg}
    if strict:
        allowed = set(ClusterConfig.model_fields.keys())
        unknown = sorted(k for k in cfg if k not in allowed) if isinstance(cfg, dict) else []
        out["unknown_keys"] = unknown
    return out
