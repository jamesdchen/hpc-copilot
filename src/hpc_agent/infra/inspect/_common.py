"""Shared snapshot-construction primitives for the inspect package.

Holds the dataclasses, the runner abstraction, the in-process TTL cache,
and the small helpers (``_is_stressed``, ``_hours_since``, ...) that
both the SLURM and SGE inspect paths need. The scheduler-specific
modules (:mod:`.slurm`, :mod:`.sge`) import from here; the package
``__init__`` re-exports the public dataclasses.
"""

from __future__ import annotations

import dataclasses
import subprocess
from typing import Any

from hpc_agent.infra.cache import TTLCache
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow

__all__ = [
    "NodeSnapshot",
    "ClusterSnapshot",
    "_CommandRunner",
    "_CACHE",
    "_hours_since",
    "_parse_gpu_count_from_tres",
    "_is_stressed",
    "_snapshot_from_dict",
]


# In-process cache so a single submit cycle that calls inspect_cluster
# multiple times pays the SSH cost once. Keyed by (cluster, scheduler).
# Stores the dict-form of :class:`ClusterSnapshot` (so re-reads survive
# even if the snapshot dataclass shape evolves between writes).
#
# Migrated to :class:`TTLCache` (B6) — same 60-second horizon as the
# pre-refactor module-level dict; gain is bounded LRU eviction + a
# ``clear_all()`` test hook shared with backfill's probe cache.
_CACHE: TTLCache[tuple[str, str], dict[str, Any]] = TTLCache(
    "infra.inspect", ttl_sec=60.0, max_size=64
)


@dataclasses.dataclass
class NodeSnapshot:
    """Per-node view used by the planner.

    Fields are best-effort: any of the numeric fields may be ``None`` when
    the scheduler did not report them. The planner treats ``None`` as
    "unknown, do not score against this signal".
    """

    name: str
    state: str = ""  # SLURM state string: IDLE / MIXED / ALLOCATED / DRAIN ...
    real_mem_mb: int | None = None
    alloc_mem_mb: int | None = None
    alloc_mem_pct: float | None = None
    cpu_tot: int | None = None
    cpu_alloc: int | None = None
    cpu_load: float | None = None  # 1-min load avg from scontrol
    cpu_load_frac: float | None = None  # cpu_load / cpu_tot, capped at None when unknown
    gres: str = ""  # advertised GRES, e.g. "gpu:a100:2"
    gres_used: str = ""  # allocated GRES, e.g. "gpu:a100:1"
    active_features: list[str] = dataclasses.field(default_factory=list)
    co_tenants: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    is_stressed: bool = False
    is_drained: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


@dataclasses.dataclass
class ClusterSnapshot:
    cluster: str
    scheduler_kind: str
    now_iso: str
    nodes: list[NodeSnapshot]
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "scheduler_kind": self.scheduler_kind,
            "now_iso": self.now_iso,
            "nodes": [n.to_dict() for n in self.nodes],
            "errors": list(self.errors),
        }


class _CommandRunner:
    """Minimal abstraction over ``ssh_run`` for unit testing.

    Tests substitute a fake runner that returns canned stdout/stderr; the
    real one shells out via ssh. We deliberately avoid threading the
    ``hpc_agent.infra.remote`` import through every call site so the
    pure parser tests don't need SSH keys.
    """

    def __init__(self, *, ssh_target: str | None, timeout: float = 60.0):
        self.ssh_target = ssh_target
        self.timeout = timeout

    def run(self, cmd: str) -> tuple[int, str, str]:
        if self.ssh_target is None:
            # Local probe — used by tests in CI that mock subprocess.
            try:
                cp = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=self.timeout,
                )
                return cp.returncode, cp.stdout or "", cp.stderr or ""
            except subprocess.TimeoutExpired as exc:
                return 124, "", f"timeout: {exc}"
            except FileNotFoundError as exc:
                return 127, "", f"missing binary: {exc}"
        from hpc_agent.infra.remote import ssh_run

        try:
            cp = ssh_run(cmd, ssh_target=self.ssh_target, timeout=self.timeout)
            return cp.returncode, cp.stdout or "", cp.stderr or ""
        except TimeoutError as exc:
            return 124, "", str(exc)
        except FileNotFoundError as exc:
            # ssh binary missing on this host — same shape as a remote
            # `command not found` so callers don't need a separate branch.
            return 127, "", f"missing binary: {exc}"
        except OSError as exc:
            # Other OS-level failures (broken pipe, etc.) — surface as a
            # generic non-zero rather than letting them propagate.
            return 1, "", f"os error: {exc}"


def _hours_since(iso_or_slurm: str) -> float | None:
    """Return hours elapsed since a SLURM-style start timestamp.

    SLURM emits ``2026-01-01T15:23:00`` (no zone). Treat as UTC for
    planning — this is "rough age" not audit-grade timing. Returns
    ``None`` on parse failure.
    """
    if not iso_or_slurm or iso_or_slurm in ("Unknown", "None"):
        return None
    ts = parse_iso_utc_or_none(iso_or_slurm)
    if ts is None:
        return None
    delta = utcnow() - ts
    return round(delta.total_seconds() / 3600.0, 2)


def _parse_gpu_count_from_tres(tres: str) -> int:
    """Re-export from ``backends.query`` to keep the parser single-sourced."""
    from hpc_agent.infra.backends.query import parse_gpu_count_from_tres

    return parse_gpu_count_from_tres(tres)


def _is_stressed(
    n: NodeSnapshot,
    stress_alloc_mem_pct: float,
    stress_cpu_load_frac: float,
) -> bool:
    if n.is_drained:
        return False  # drained is reported separately, not as stressed
    if n.alloc_mem_pct is not None and n.alloc_mem_pct >= stress_alloc_mem_pct:
        return True
    return bool(n.cpu_load_frac is not None and n.cpu_load_frac >= stress_cpu_load_frac)


def _snapshot_from_dict(d: dict[str, Any]) -> ClusterSnapshot:
    nodes = [NodeSnapshot(**{**n}) for n in d.get("nodes", [])]
    return ClusterSnapshot(
        cluster=d["cluster"],
        scheduler_kind=d["scheduler_kind"],
        now_iso=d["now_iso"],
        nodes=nodes,
        errors=list(d.get("errors", [])),
    )
