"""Public API contract — pin the externally-visible surface of ``hpc_agent``.

PR 0a of the large reorg. This test exists so the file-move PRs that
follow cannot silently break what downstream callers import.

Three surfaces are pinned:

* ``hpc_agent.__all__`` — the package root re-exports.
* ``hpc_agent.errors`` — the typed exception hierarchy.
* ``hpc_agent.integration`` — wire constants for harness authors.

Anything NOT in these snapshots is considered private and is free to
move during the reorg. If you genuinely need a new public name, add it
to the underlying ``__all__`` AND to the matching constant below in
the same PR — that is the deliberate friction this contract creates.
"""

from __future__ import annotations

import hpc_agent
import hpc_agent.errors
import hpc_agent.integration

# ---------------------------------------------------------------------------
# Snapshots — edit these only when you are intentionally changing the public
# API. Each change must ship with a version bump and a CHANGELOG entry.
# ---------------------------------------------------------------------------

EXPECTED_ALL: frozenset[str] = frozenset(
    {
        "ClusterConstraints",
        "ExecutorInfo",
        "JournalLayout",
        "MAX_RUNS",
        "PrimitiveMeta",
        "RUNS_SUBDIR",
        "RepoLayout",
        "ResubmitBatch",
        "ResubmitPlan",
        "SIDECAR_SCHEMA_VERSION",
        "SideEffect",
        "SubmissionPlan",
        "TASKS_FILENAME",
        "WorkloadSpec",
        "_PACKAGE_ROOT",
        "__version__",
        "append_runtime_sample",
        "build_wave_map",
        "check_results",
        "check_results_from_tasks",
        "classify_failure",
        "compact_task_ids",
        "compute_cmd_sha",
        "compute_submission_plan",
        "compute_tasks_py_sha",
        "deploy_runtime",
        "detect_scheduler",
        "discover_executors",
        "find_existing_runs",
        "find_run_by_cmd_sha",
        "get_meta",
        "get_registry",
        "get_template_path",
        "inspect_cluster",
        "is_executor_source",
        "load_clusters_config",
        "load_tasks_module",
        "parse_constraints",
        "pick_gpu",
        "primitive",
        "prune_old_runs",
        "read_run_sidecar",
        "reduce_by_grid_point",
        "reduce_metrics",
        "reduce_partials",
        "reduce_resource_usage",
        "register_primitives",
        "report_status",
        "report_status_from_tasks",
        "resubmit_plan",
        "roll_up_runtime_quantiles",
        "rollup_by_grid_point",
        "rsync_pull",
        "rsync_push",
        "run_combiner",
        "run_combiner_checked",
        "run_sidecar_path",
        "ssh_run",
        "write_metrics",
        "write_run_sidecar",
    }
)

EXPECTED_ERRORS: frozenset[str] = frozenset(
    {
        "ClusterPartiallyDegraded",
        "ClusterTimeout",
        "ClusterUnknown",
        "CombinerFailed",
        "ConfigInvalid",
        "ExecutorNotFound",
        "HpcError",
        "JournalCorrupt",
        "OutputsMissing",
        "PreconditionFailed",
        "Preempted",
        "RemoteCommandFailed",
        "SchedulerThrottled",
        "SchemaIncompat",
        "SpecInvalid",
        "SshUnreachable",
        # ``from __future__ import annotations`` leaks ``annotations`` into
        # ``dir(module)``. Snapshotted verbatim so a future cleanup (e.g.
        # ``del annotations`` or dropping the future import) is a deliberate
        # public-surface change, not an accidental one.
        "annotations",
    }
)

EXPECTED_INTEGRATION: frozenset[str] = frozenset(
    {
        "CLUSTERS_CONFIG_ENV",
        "ERROR_CODES",
        "HPC_KW_PREFIX",
        "JOURNAL_DIR_ENV",
        "LIFECYCLE_STATES",
        "LOCAL_DATA_DIR_ENV",
        "RESULT_DIR_ENV",
        # See note in EXPECTED_ERRORS — ``annotations`` is a __future__ leak.
        "annotations",
    }
)


_CONTRACT_HINT = (
    "\n\nAnything NOT in this contract is private and free to move during the "
    "reorg. If you need a new public name, add it to the underlying __all__ "
    "AND to this contract in the same PR (and bump the package version)."
)


def _public_names(module) -> frozenset[str]:
    """Names visible via ``dir(module)`` minus dunders and underscore-private."""
    return frozenset(n for n in dir(module) if not n.startswith("_"))


def test_hpc_agent_all_is_pinned() -> None:
    actual = frozenset(hpc_agent.__all__)
    added = sorted(actual - EXPECTED_ALL)
    removed = sorted(EXPECTED_ALL - actual)
    assert actual == EXPECTED_ALL, (
        f"hpc_agent.__all__ drifted from the pinned public API.\n"
        f"  unexpectedly added : {added}\n"
        f"  unexpectedly removed: {removed}"
        f"{_CONTRACT_HINT}"
    )


def test_hpc_agent_errors_public_surface_is_pinned() -> None:
    actual = _public_names(hpc_agent.errors)
    added = sorted(actual - EXPECTED_ERRORS)
    removed = sorted(EXPECTED_ERRORS - actual)
    assert actual == EXPECTED_ERRORS, (
        f"hpc_agent.errors public surface drifted from the pinned contract.\n"
        f"  unexpectedly added : {added}\n"
        f"  unexpectedly removed: {removed}"
        f"{_CONTRACT_HINT}"
    )


def test_hpc_agent_integration_public_surface_is_pinned() -> None:
    actual = _public_names(hpc_agent.integration)
    added = sorted(actual - EXPECTED_INTEGRATION)
    removed = sorted(EXPECTED_INTEGRATION - actual)
    assert actual == EXPECTED_INTEGRATION, (
        f"hpc_agent.integration public surface drifted from the pinned contract.\n"
        f"  unexpectedly added : {added}\n"
        f"  unexpectedly removed: {removed}"
        f"{_CONTRACT_HINT}"
    )
