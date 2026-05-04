"""Order-book-style queue features derived from a single cluster snapshot.

Phase 1b of the queue-wait predictor plan: turn the raw
:class:`~hpc_mapreduce.infra.inspect.ClusterSnapshot` into a fixed-shape
:class:`QueueFeatures` record that the predictor can fold in alongside
the diurnal moving-average baseline.

The features are deliberately *submit-time* signals — what the queue
looks like at the moment of submission — rather than time-series
aggregates. Time-series aggregates live in
``cluster_history`` callers that read multiple snapshots; this module
only consumes one snapshot.

Permissive on missing inputs: a node missing CPU/memory metadata
contributes zeros to the aggregates rather than raising. The features
are advisory; refusing to compute them is worse than computing partial
ones.

GPU advertisements vs. demand
-----------------------------
- ``gpu_type_supply`` reads each node's ``gres`` field (``gpu:a100:2``
  → {"a100": 2}).
- ``gpu_type_running`` reads ``gres_used`` (``gpu:a100:1`` → {"a100":
  1}). For untyped supply (``gpu:2``) the type is recorded as
  ``"unknown"``.
- ``gpu_type_queued_demand`` is computed from co-tenant rows whose
  state indicates pending allocation (``PD``/``PENDING``). Most clusters
  surface only running co-tenants in the snapshot, so this is often
  zero — that's a known limitation noted in the field's docstring.

Partition resolution
--------------------
The snapshot does not currently carry per-node ``Partitions=...`` fields
(they're available in raw ``scontrol show node`` but are not parsed).
``queued_jobs_in_partition`` therefore degrades to "queued jobs we know
about" when ``partition`` is supplied but no node carries partition
metadata. Phase 2+ may extend the snapshot parser to carry partition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_mapreduce._time import parse_iso_utc_or_none, utcnow

if TYPE_CHECKING:
    from hpc_mapreduce.infra.inspect import ClusterSnapshot, NodeSnapshot

__all__ = ["QueueFeatures", "compute_features"]


_PENDING_STATES = {"PD", "PENDING", "QUEUED", "qw"}


@dataclass(frozen=True)
class QueueFeatures:
    """Submit-time order-book features for a cluster.

    All counts are best-effort — partial scheduler responses degrade to
    zero rather than raising. ``snapshot_age_sec`` is useful for the
    predictor to discount features computed off a stale snapshot.
    """

    # At-snapshot cluster state
    queued_jobs_total: int
    running_jobs_total: int
    # Per-partition queue depth (resolves to scheduler partition).
    # Falls back to overall counts when partition metadata is unavailable.
    queued_jobs_in_partition: int
    running_jobs_in_partition: int
    # GPU-type supply/demand (typed buckets; ``"unknown"`` for untyped).
    gpu_type_supply: dict[str, int] = field(default_factory=dict)
    gpu_type_running: dict[str, int] = field(default_factory=dict)
    gpu_type_queued_demand: dict[str, int] = field(default_factory=dict)
    # Resource pressure (cluster-wide weighted means).
    cpus_in_use_pct: float = 0.0
    mem_in_use_pct: float = 0.0
    # Submission-time signals
    n_unique_users_running: int = 0
    snapshot_age_sec: int = 0


def _parse_gres_typed(gres: str) -> dict[str, int]:
    """Parse a SLURM-style GRES string into ``{type: count}``.

    Accepts ``gpu:a100:2``, ``gpu:2``, ``gpu:a100:2,gpu:v100:1``. The
    untyped form bucketises under ``"unknown"`` so a cluster that
    advertises only ``gpu:N`` still contributes to the aggregate.

    Permissive: malformed entries are skipped rather than raising. The
    parser intentionally rejects the SLURM ``(IDX:0-7)`` enumeration
    suffix that ``GresUsed`` sometimes carries — the count comes from
    the ``:N`` field, not from the index list.
    """
    if not gres:
        return {}
    out: dict[str, int] = {}
    for raw in gres.split(","):
        raw = raw.strip()
        if not raw:
            continue
        # Strip trailing "(IDX:0-3)" or similar.
        raw = re.sub(r"\(.*\)$", "", raw)
        if not raw.startswith("gpu"):
            continue
        parts = raw.split(":")
        # Forms: ['gpu', 'N']  or  ['gpu', 'type', 'N']  or ['gpu']
        if len(parts) == 2:
            try:
                n = int(parts[1])
            except ValueError:
                continue
            out["unknown"] = out.get("unknown", 0) + n
        elif len(parts) >= 3:
            gpu_type = parts[1] or "unknown"
            try:
                n = int(parts[2])
            except ValueError:
                continue
            out[gpu_type] = out.get(gpu_type, 0) + n
    return out


def _add_into(dst: dict[str, int], src: dict[str, int]) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _node_partitions(node: NodeSnapshot) -> list[str]:
    """Best-effort partition lookup. Currently no partition data lives on
    :class:`NodeSnapshot`; returns ``[]`` so the partition filter
    degrades to "all nodes" consistently. Wired here so a future
    snapshot-parser extension can populate partitions without changing
    callers.
    """
    return getattr(node, "partitions", []) or []


def _is_pending_state(state: str) -> bool:
    if not state:
        return False
    return state.upper() in _PENDING_STATES or state in _PENDING_STATES


def compute_features(
    snap: ClusterSnapshot,
    *,
    partition: str | None = None,
) -> QueueFeatures:
    """Derive submit-time features from a single cluster snapshot.

    *partition* (optional): when given, ``queued_jobs_in_partition`` and
    ``running_jobs_in_partition`` are restricted to nodes whose
    ``partitions`` attribute contains the value. Snapshots that do not
    carry partition metadata fall back to the cluster-wide counts so
    downstream callers always get a usable feature.
    """
    queued_total = 0
    running_total = 0
    queued_in_part = 0
    running_in_part = 0

    gpu_supply: dict[str, int] = {}
    gpu_running: dict[str, int] = {}
    gpu_queued_demand: dict[str, int] = {}

    cpu_alloc_sum = 0
    cpu_tot_sum = 0
    mem_alloc_sum = 0
    mem_tot_sum = 0

    users_running: set[str] = set()

    any_node_has_partitions = False

    for node in snap.nodes:
        if node.is_drained:
            # Drained nodes contribute neither supply nor demand. They
            # still receive co-tenant rows (long-running jobs from
            # before the drain), but those are stale by definition.
            continue
        # Supply
        _add_into(gpu_supply, _parse_gres_typed(node.gres))
        _add_into(gpu_running, _parse_gres_typed(node.gres_used))
        # Resource-pressure aggregates.
        if node.cpu_tot is not None:
            cpu_tot_sum += int(node.cpu_tot)
        if node.cpu_alloc is not None:
            cpu_alloc_sum += int(node.cpu_alloc)
        if node.real_mem_mb is not None:
            mem_tot_sum += int(node.real_mem_mb)
        if node.alloc_mem_mb is not None:
            mem_alloc_sum += int(node.alloc_mem_mb)

        node_partitions = _node_partitions(node)
        if node_partitions:
            any_node_has_partitions = True
        in_partition = (
            partition is None or partition in node_partitions
        )

        for tenant in node.co_tenants:
            state = str(tenant.get("state") or "")
            user = str(tenant.get("user") or "")
            tenant_gpus = tenant.get("gpus") or 0
            if _is_pending_state(state):
                queued_total += 1
                if in_partition:
                    queued_in_part += 1
                # Co-tenants do not carry GPU type — bucket as 'unknown'.
                if tenant_gpus:
                    gpu_queued_demand["unknown"] = (
                        gpu_queued_demand.get("unknown", 0) + int(tenant_gpus)
                    )
            else:
                # Treat anything non-pending as running. Terminal-state
                # rows are filtered out upstream by the snapshot
                # builder; this stays robust to wider future state
                # vocabularies.
                running_total += 1
                if in_partition:
                    running_in_part += 1
                if user:
                    users_running.add(user)

    # If partition was requested but the snapshot carries no partition
    # metadata at all, fall back to the cluster-wide counts so the
    # feature is always usable. The predictor will see the same value
    # for "in-partition" and "total", which still beats returning zero.
    if partition is not None and not any_node_has_partitions:
        queued_in_part = queued_total
        running_in_part = running_total

    cpus_pct = (cpu_alloc_sum / cpu_tot_sum) if cpu_tot_sum > 0 else 0.0
    mem_pct = (mem_alloc_sum / mem_tot_sum) if mem_tot_sum > 0 else 0.0

    age_sec = 0
    snap_dt = parse_iso_utc_or_none(snap.now_iso)
    if snap_dt is not None:
        delta = (utcnow() - snap_dt).total_seconds()
        age_sec = max(0, int(delta))

    return QueueFeatures(
        queued_jobs_total=queued_total,
        running_jobs_total=running_total,
        queued_jobs_in_partition=queued_in_part,
        running_jobs_in_partition=running_in_part,
        gpu_type_supply=dict(sorted(gpu_supply.items())),
        gpu_type_running=dict(sorted(gpu_running.items())),
        gpu_type_queued_demand=dict(sorted(gpu_queued_demand.items())),
        cpus_in_use_pct=round(cpus_pct, 4),
        mem_in_use_pct=round(mem_pct, 4),
        n_unique_users_running=len(users_running),
        snapshot_age_sec=age_sec,
    )


def _to_dict(features: QueueFeatures) -> dict[str, Any]:
    """Helper: dataclass → dict for serialization. Public-ish for tests."""
    from dataclasses import asdict

    return asdict(features)
