"""Job definition modules: discovery, constraints, throughput, runs."""

from claude_hpc.orchestrator.planning.constraints import ClusterConstraints, parse_constraints
from claude_hpc.orchestrator.planning.throughput import (
    JobBatch,
    SubmissionPlan,
    WorkloadSpec,
    compute_submission_plan,
)
from claude_hpc.orchestrator.state.discover import (
    ExecutorInfo,
    discover_executors,
    is_executor_source,
)
from claude_hpc.orchestrator.state.runs import (
    MAX_RUNS,
    SIDECAR_SCHEMA_VERSION,
    compute_cmd_sha,
    compute_tasks_py_sha,
    find_existing_runs,
    find_run_by_cmd_sha,
    prune_old_runs,
    read_run_sidecar,
    run_sidecar_path,
    write_run_sidecar,
)

__all__ = [
    "ClusterConstraints",
    "parse_constraints",
    "ExecutorInfo",
    "discover_executors",
    "is_executor_source",
    "MAX_RUNS",
    "SIDECAR_SCHEMA_VERSION",
    "compute_cmd_sha",
    "compute_tasks_py_sha",
    "find_existing_runs",
    "find_run_by_cmd_sha",
    "prune_old_runs",
    "read_run_sidecar",
    "run_sidecar_path",
    "write_run_sidecar",
    "WorkloadSpec",
    "JobBatch",
    "SubmissionPlan",
    "compute_submission_plan",
]
