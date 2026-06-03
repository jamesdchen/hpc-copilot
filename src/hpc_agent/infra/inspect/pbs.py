"""PBS (PBS Pro / OpenPBS + TORQUE) cluster inspection.

A deliberately *minimal* snapshot. PBS exposes node/resource state via
``pbsnodes -av``, but that output diverges between PBS Pro and TORQUE and
isn't safely verifiable without a live cluster, so node-level backfill
data is left unpopulated rather than guessed. The planner treats missing
fields as "unknown" (conservative) rather than "fine", so submit + live
monitoring proceed normally; only the backfill / throughput
*optimisation* is unavailable for PBS until a verified ``pbsnodes`` parser
lands (a data/parser follow-up best done against a real PBS cluster).

Returning a structurally-valid snapshot here (instead of raising) is what
makes a PBS cluster's ``inspect`` / planning path degrade safely.
"""

from __future__ import annotations

from typing import Any

from hpc_agent.infra.time import utcnow_iso

from ._common import ClusterSnapshot, _CommandRunner

__all__ = ["_pbs_inspect"]


def _pbs_inspect(
    cluster_name: str,
    cluster_cfg: dict[str, Any],
    *,
    scheduler_kind: str = "pbspro",
    stress_alloc_mem_pct: float,
    stress_cpu_load_frac: float,
    runner: _CommandRunner,
) -> ClusterSnapshot:
    """Return a valid, minimal :class:`ClusterSnapshot` for a PBS cluster.

    No node-level data yet (see module docstring); the single diagnostic
    note makes the absence explicit rather than looking like a zero-capacity
    cluster, and the planner falls back to conservative defaults.
    """
    return ClusterSnapshot(
        cluster=cluster_name,
        scheduler_kind=scheduler_kind,
        now_iso=utcnow_iso(),
        nodes=[],
        errors=[
            {
                "code": "pbs_inspect_minimal",
                "detail": (
                    "PBS node-level snapshot is not yet populated (pbsnodes "
                    "parsing differs PBS Pro vs TORQUE and needs a live cluster "
                    "to verify); planner uses conservative defaults. Submit and "
                    "live monitoring are unaffected."
                ),
            }
        ],
    )
