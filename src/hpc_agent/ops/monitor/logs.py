"""Per-task log fetching — re-exports the canonical implementation
from ``infra/cluster_logs.py`` so the recover subject (failures atom)
can reach it without crossing into the monitor subject.

The function is kept under this import path for the ``ops/monitor``-
internal callers (the ``logs_atom``); cross-subject callers should
import directly from ``hpc_agent.infra.cluster_logs``.
"""

from __future__ import annotations

from hpc_agent.infra.cluster_logs import fetch_task_logs

__all__ = ["fetch_task_logs"]
