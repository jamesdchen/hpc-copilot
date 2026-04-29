"""Job definition modules: discovery, constraints, throughput, runs.

The ``grid`` and ``manifest`` re-exports below remain during the
migration to the ``.hpc/tasks.py`` model; they will be dropped once
consumers (slash commands, CLI, tests) are fully ported.
"""

from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints
from hpc_mapreduce.job.discover import (
    ExecutorInfo,
    discover_executors,
    is_executor_source,
)
from hpc_mapreduce.job.grid import (
    build_task_manifest,
    expand_grid,
    resolve_git_sha,
    total_tasks,
    validate_result_dir_template,
)
from hpc_mapreduce.job.manifest import (
    MANIFEST_ALIAS,
    MAX_MANIFESTS,
    aggregate_cmd_sha,
    build_manifest_with_resume,
    find_existing_manifests,
    find_manifest_by_cmd_sha,
    manifest_filename_for_sha,
    prune_old_manifests,
    write_manifest,
)
from hpc_mapreduce.job.runs import (
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
from hpc_mapreduce.job.throughput import (
    JobBatch,
    SubmissionPlan,
    WorkloadSpec,
    compute_submission_plan,
)

__all__ = [
    "ClusterConstraints",
    "parse_constraints",
    "ExecutorInfo",
    "discover_executors",
    "is_executor_source",
    # Legacy grid/manifest API — pending removal
    "expand_grid",
    "build_task_manifest",
    "total_tasks",
    "resolve_git_sha",
    "validate_result_dir_template",
    "MAX_MANIFESTS",
    "MANIFEST_ALIAS",
    "manifest_filename_for_sha",
    "aggregate_cmd_sha",
    "write_manifest",
    "find_existing_manifests",
    "find_manifest_by_cmd_sha",
    "prune_old_manifests",
    "build_manifest_with_resume",
    # Per-run sidecar API
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
