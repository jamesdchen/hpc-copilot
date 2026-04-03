"""Reduce-phase modules: metrics aggregation and status reporting."""

from hpc_mapreduce.reduce.metrics import reduce_metrics
from hpc_mapreduce.reduce.status import (
    check_results,
    detect_scheduler,
    get_err_log_paths,
    reduce_counters,
    report_status,
)

__all__ = [
    "reduce_metrics",
    "check_results",
    "reduce_counters",
    "report_status",
    "get_err_log_paths",
    "detect_scheduler",
]
