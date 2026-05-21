"""``inspect-cluster`` primitive — plugin-owned registry wrapper.

The compute function (``hpc_agent.infra.inspect.inspect_cluster``) stays
in the public ``hpc-agent`` package; the public package drops its
``@primitive`` decorator as part of the scheduling-strategy extraction.
This module re-attaches the decorator so the plugin owns the registry
entry. The wrapper signature mirrors the original verbatim so existing
callers and tests are unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent.infra.inspect import inspect_cluster as _inspect_cluster

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.infra.inspect import ClusterSnapshot
    from hpc_agent.infra.inspect._common import _CommandRunner

__all__ = ["inspect_cluster"]


@primitive(
    name="inspect-cluster",
    verb="query",
    side_effects=[SideEffect("ssh", "<cluster>")],
    error_codes=[errors.ClusterUnknown, errors.SshUnreachable],
    idempotent=True,
    idempotency_key="cluster",
    cli="hpc-agent inspect-cluster --cluster <name> [...]",
)
def inspect_cluster(
    cluster_name: str,
    *,
    config_path: str | Path | None = None,
    sacct_window_hours: int = 24,
    stress_alloc_mem_pct: float = 0.80,
    stress_cpu_load_frac: float = 0.80,
    use_cache: bool = True,
    runner: _CommandRunner | None = None,
    persist_dir: Path | None = None,
) -> ClusterSnapshot:
    """Return a :class:`ClusterSnapshot` for *cluster_name*.

    Thin pass-through to ``hpc_agent.infra.inspect.inspect_cluster``;
    see that function for the full behaviour contract.
    """
    return _inspect_cluster(
        cluster_name,
        config_path=config_path,
        sacct_window_hours=sacct_window_hours,
        stress_alloc_mem_pct=stress_alloc_mem_pct,
        stress_cpu_load_frac=stress_cpu_load_frac,
        use_cache=use_cache,
        runner=runner,
        persist_dir=persist_dir,
    )
