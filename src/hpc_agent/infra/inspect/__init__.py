"""Cluster node inspection — read-only snapshot of node states for planning.

For each cluster, query the scheduler (SLURM ``scontrol``/``sacct`` or SGE
``qhost``/``qstat``) to assemble a structured per-node view that captures
the ingredients for resource-quality-aware submission decisions:

- Allocation pressure (host RAM, CPU load).
- GPU advertisements (advertised GRES vs. allocated GRES).
- Co-tenant context — other users' jobs running on the node, with their
  resource shares and how long they've been running.
- Drain / down state.

The resulting JSON is fed into :mod:`hpc_agent.planning.planner` (Phase 4)
which combines it with runtime priors to score candidate constraints.
It is also useful standalone for ad-hoc cluster
debugging via ``hpc-agent inspect-cluster --cluster <c>``.

This module is intentionally permissive: scheduler outputs vary between
versions and configurations. Parsing failures degrade to "unknown" /
zero-valued fields rather than raising — better to deliver a partial
snapshot than to refuse to plan at all.

Package layout
--------------

This used to be a single ~900-LOC module; it now splits into:

- :mod:`._common` — dataclasses, in-process cache, runner abstraction,
  and small helpers shared by every backend.
- :mod:`.slurm` — SLURM-specific parsers (``parse_scontrol_show_node``,
  ``parse_sacct_node_jobs``) and the ``_slurm_inspect`` driver.
- :mod:`.sge` — SGE-specific parsers (``_parse_qhost``,
  ``_parse_qstat_full``) and the ``_sge_inspect`` driver.
- :mod:`._persist` — ``ClusterSnapshot`` history persistence
  (``persist_snapshot``, ``read_cluster_history``).

The public surface (``inspect_cluster``, the dataclasses, and the
already-exported parsers) is preserved verbatim through this package's
``__init__``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from hpc_agent import errors
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent.infra.clusters import load_clusters_config

from ._common import (
    _CACHE,
    ClusterSnapshot,
    NodeSnapshot,
    _CommandRunner,
    _hours_since,
    _is_stressed,
    _parse_gpu_count_from_tres,
    _snapshot_from_dict,
)
from ._persist import (
    MAX_HISTORY_SNAPSHOTS,
    persist_snapshot,
    read_cluster_history,
)
from .sge import _parse_qhost, _parse_qstat_full, _sge_inspect
from .slurm import (
    _bucket_tenants_by_node,
    _expand_slurm_nodelist,
    _slurm_inspect,
    parse_sacct_node_jobs,
    parse_scontrol_show_node,
)

__all__ = [
    # public dataclasses
    "NodeSnapshot",
    "ClusterSnapshot",
    # public entry
    "inspect_cluster",
    # public parsers (kept as documented API)
    "parse_scontrol_show_node",
    "parse_sacct_node_jobs",
    # history persistence
    "persist_snapshot",
    "read_cluster_history",
    "MAX_HISTORY_SNAPSHOTS",
]


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

    Reads ``clusters.yaml`` to determine SSH target and scheduler kind.
    Caches the result for 60s in-process so a single submit cycle that
    re-asks (e.g. after a canary run) doesn't pay the SSH cost twice.

    Stress thresholds are tunable so the planner can experiment with
    cost-function knobs without code changes.
    """
    clusters = load_clusters_config(Path(config_path) if config_path is not None else None)
    if cluster_name not in clusters:
        raise errors.ClusterUnknown(f"unknown cluster {cluster_name!r}; check clusters.yaml")
    cfg = clusters[cluster_name]
    scheduler = (cfg.get("scheduler") or "slurm").lower()
    cache_key = (cluster_name, scheduler)
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return _snapshot_from_dict(cached)
    if runner is None:
        runner = _CommandRunner(ssh_target=cfg.get("ssh_target"))
    # B5-PR2: dispatch through the backend registry. Each backend's
    # ``inspect_cluster`` classmethod normalises kwargs for its scheduler
    # (e.g. SGE ignores ``sacct_window_hours``); a missing backend
    # raises ValueError just like the prior ladder did.
    from hpc_agent.infra.backends import get_backend_class

    try:
        backend_cls = get_backend_class(scheduler)
    except ValueError as exc:
        raise ValueError(
            f"unsupported scheduler {scheduler!r} for cluster {cluster_name!r}"
        ) from exc
    snap: ClusterSnapshot = backend_cls.inspect_cluster(
        cluster_name,
        cfg,
        sacct_window_hours=sacct_window_hours,
        stress_alloc_mem_pct=stress_alloc_mem_pct,
        stress_cpu_load_frac=stress_cpu_load_frac,
        runner=runner,
    )
    if use_cache:
        _CACHE.put(cache_key, snap.to_dict())
    if persist_dir is not None:
        # Best-effort: a snapshot persistence failure must not blow up
        # the planning pipeline. We only emit the file under a real
        # experiment dir; tests pass tmp_path directly.
        with contextlib.suppress(OSError):
            persist_snapshot(persist_dir, snap)
    return snap


# Underscore re-exports retained ONLY for tests that monkeypatch the
# inspect package (``monkeypatch.setattr('hpc_agent.infra.inspect._sge_inspect', ...)``).
# New runtime code (including sibling backends) imports from the
# submodule directly — see ``infra/backends/sge.py`` and
# ``infra/backends/slurm.py``. Treat these names as deprecated public
# API; they may move behind a ``DeprecationWarning`` in a later release.
__all__ += [
    "_CACHE",
    "_CommandRunner",
    "_bucket_tenants_by_node",
    "_expand_slurm_nodelist",
    "_hours_since",
    "_is_stressed",
    "_parse_gpu_count_from_tres",
    "_parse_qhost",
    "_parse_qstat_full",
    "_sge_inspect",
    "_slurm_inspect",
    "_snapshot_from_dict",
]
