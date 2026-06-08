"""Reduce-phase modules: metrics aggregation and status reporting."""

from hpc_agent.execution.mapreduce.reduce.metrics import (
    reduce_by_grid_point,
    reduce_metrics,
    reduce_partials,
)
from hpc_agent.execution.mapreduce.reduce.status import (
    check_results,
    detect_scheduler,
    get_err_log_paths,
    report_status,
)

__all__ = [
    "reduce_metrics",
    "reduce_by_grid_point",
    "reduce_partials",
    "check_results",
    "report_status",
    "get_err_log_paths",
    "detect_scheduler",
]
