"""Job definition modules: grid expansion and cluster constraints."""

from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints
from hpc_mapreduce.job.grid import (
    build_task_manifest,
    expand_backtest,
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
from hpc_mapreduce.job.throughput import (
    JobBatch,
    SubmissionPlan,
    WorkloadSpec,
    compute_submission_plan,
)

__all__ = [
    "ClusterConstraints",
    "parse_constraints",
    "expand_grid",
    "expand_backtest",
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
    "WorkloadSpec",
    "JobBatch",
    "SubmissionPlan",
    "compute_submission_plan",
]
