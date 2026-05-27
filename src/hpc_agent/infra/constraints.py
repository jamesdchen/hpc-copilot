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
    est_spin_up:
        Estimated per-array queue spin-up overhead (e.g. ``"5m"``).
    max_tasks:
        Optional *advisory* ceiling on total tasks for a single submission.
        Surfaced by ``/submit`` to confirm large grids with the user.  This
        is **not** enforced by the throughput optimizer — it is a soft hint.
        ``None`` disables the advisory.
    """

    max_array_size: int = 1000
    max_walltime: str = "24:00:00"
    max_concurrent_jobs: int = 10
    est_spin_up: str = "5m"
    max_tasks: int | None = None

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
