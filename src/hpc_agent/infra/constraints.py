# @pure: no-io
"""Cluster constraint declarations for throughput optimization."""

from __future__ import annotations

import dataclasses
import re

from hpc_agent import errors
from hpc_agent.infra.parsing import parse_walltime_to_sec

__all__ = ["ClusterConstraints", "parse_constraints"]


@dataclasses.dataclass(frozen=True)
class ClusterConstraints:
    """Declared cluster constraints for throughput optimization.

    These constraints describe scheduler limits and overhead that a
    throughput optimizer can use to plan job submissions.

    Fields
    ------
    max_array_size:
        Maximum task count per scheduler array (hard scheduler limit).
    max_walltime:
        Maximum wall-clock time allowed per job (``HH:MM:SS``).
    max_concurrent_jobs:
        Maximum number of arrays that may run simultaneously.
    max_concurrent_tasks:
        Optional scheduler-native in-array concurrency cap (#339 item 16) — how
        many TASKS of a single array may run at once, spelled ``--array=..%N``
        (SLURM) / ``qsub -tc N`` (UGE). This bounds concurrency WITHOUT the
        ``afterany`` wave boundary that drains to ~zero while stragglers finish
        (run #11: the human watched concurrency fall 76→20 with no back-fill);
        the scheduler back-fills the array as tasks complete. ``None`` (the
        default) disables it — **off by default, no behavior change**: a sweep
        that fits in one array submits an uncapped array exactly as before, and a
        multi-array sweep keeps its ``afterany`` wave chain. Set it and a
        single-array sweep gains the native cap; a multi-array sweep additionally
        caps concurrency within each wave's arrays.
    est_spin_up:
        Estimated per-array queue spin-up overhead (e.g. ``"5m"``).
    max_tasks:
        Optional *advisory* ceiling on total tasks for a single submission.
        Surfaced by ``/submit`` to confirm large grids with the user.  This
        is **not** enforced by the throughput optimizer — it is a soft hint.
        ``None`` disables the advisory.
    max_estimated_core_hours:
        Optional per-cluster compute-cost threshold for the #345 cost/scale
        gate (estimated ``tasks × walltime × cores`` core-hours). When the
        pre-dispatch estimate crosses this, the submit gate requires explicit
        confirmation (interactive) or refuses with ``spec_invalid``
        (unattended, unless under ``HPC_AGENT_COST_BUDGET``). ``None``
        (the default) disables the gate entirely — **off by default, no
        behavior change** until an operator sets it.
    """

    max_array_size: int = 1000
    max_walltime: str = "24:00:00"
    max_concurrent_jobs: int = 10
    est_spin_up: str = "5m"
    max_tasks: int | None = None
    max_estimated_core_hours: float | None = None
    max_concurrent_tasks: int | None = None

    def walltime_seconds(self) -> int:
        """Parse max_walltime HH:MM:SS to total seconds."""
        return parse_walltime_to_sec(self.max_walltime)

    def spin_up_seconds(self) -> int:
        """Parse est_spin_up duration string (e.g. '5m', '30s', '1h') to seconds."""
        m = re.fullmatch(r"(\d+)\s*([smh]?)", self.est_spin_up.strip().lower())
        if not m:
            raise errors.SpecInvalid(f"Cannot parse spin-up duration: {self.est_spin_up!r}")
        val = int(m.group(1))
        unit = m.group(2) or "s"
        return val * {"s": 1, "m": 60, "h": 3600}[unit]


def parse_constraints(raw: dict) -> ClusterConstraints:
    """Build a ClusterConstraints from a raw config dict.

    Unknown keys are silently ignored so configs can evolve.
    """
    known = {f.name for f in dataclasses.fields(ClusterConstraints)}
    filtered = {k: v for k, v in raw.items() if k in known}
    return ClusterConstraints(**filtered)
