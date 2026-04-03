"""Cluster constraint declarations for throughput optimization."""

from __future__ import annotations

import dataclasses
import re

__all__ = ["ClusterConstraints", "parse_constraints"]


@dataclasses.dataclass(frozen=True)
class ClusterConstraints:
    """Declared cluster constraints for throughput optimization.

    These constraints describe scheduler limits and overhead that a
    throughput optimizer can use to plan job submissions.
    """
    max_array_size: int = 1000
    max_walltime: str = "24:00:00"
    max_concurrent_jobs: int = 10
    est_spin_up: str = "5m"

    def walltime_seconds(self) -> int:
        """Parse max_walltime HH:MM:SS to total seconds."""
        parts = self.max_walltime.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0])

    def spin_up_seconds(self) -> int:
        """Parse est_spin_up duration string (e.g. '5m', '30s', '1h') to seconds."""
        m = re.fullmatch(r"(\d+)\s*([smh]?)", self.est_spin_up.strip().lower())
        if not m:
            raise ValueError(f"Cannot parse spin-up duration: {self.est_spin_up!r}")
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
