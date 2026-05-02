"""Allowlist-style enforcement of the claude-hpc boundary contract.

Each test below declares what IS permitted (an allowlist) and asserts that
reality matches in both directions. Failures print actionable diffs that
point back to ``docs/boundary-contract.md`` — the single source of truth for
the boundary between the framework and experiment repos.

Stdlib only (``ast``, ``pathlib``) plus ``yaml`` (already a project
dependency, see ``pyproject.toml``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

import hpc_mapreduce
from hpc_mapreduce.job.discover import _SKIP_BASENAMES, _SKIP_DIRS

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DOC = "docs/boundary-contract.md"


# ---------------------------------------------------------------------------
# Allowlists — keep in sync with docs/boundary-contract.md
# ---------------------------------------------------------------------------

ALLOWED_EXPORTS = frozenset(
    {
        # Package root
        "_PACKAGE_ROOT",
        "__version__",
        # Config & discovery
        "load_clusters_config",
        "get_template_path",
        # Framework subdirectory layout (NEW: .hpc/tasks.py model)
        "HPC_SUBDIR",
        "TASKS_FILENAME",
        "RUNS_SUBDIR",
        "framework_subdir",
        "runs_subdir",
        "tasks_path",
        "load_tasks_module",
        # Per-run sidecars (NEW)
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
        # Remote execution
        "ssh_run",
        "rsync_push",
        "rsync_pull",
        "deploy_runtime",
        # Job status & results
        "check_results",
        "check_results_from_tasks",
        "report_status",
        "report_status_from_tasks",
        "rollup_by_grid_point",
        "detect_scheduler",
        # GPU selection
        "pick_gpu",
        # Reduce
        "reduce_metrics",
        "reduce_by_grid_point",
        "reduce_partials",
        "reduce_resource_usage",
        "classify_failure",
        # Executor discovery
        "ExecutorInfo",
        "discover_executors",
        "is_executor_source",
        # Cluster constraints
        "ClusterConstraints",
        "parse_constraints",
        # Throughput optimizer
        "WorkloadSpec",
        "SubmissionPlan",
        "compute_submission_plan",
        "build_wave_map",
        # Smart-submit data layer
        "inspect_cluster",
        "record_segv",
        "get_active_blacklist",
        "append_runtime_sample",
        "roll_up_runtime_quantiles",
        "plan_submit",
        # Resubmit
        "compact_task_ids",
        "ResubmitBatch",
        "ResubmitPlan",
        "resubmit_plan",
        # Combiner
        "run_combiner",
        "run_combiner_checked",
        # Per-task metrics sidecar
        "write_metrics",
    }
)

RESERVED_FILES = frozenset(
    {
        # Python package convention; not a framework reservation but the
        # discovery scanner skips it so it stays out of executor candidates.
        "__init__.py",
    }
)

# Reserved DIRECTORIES inside experiment repos. The discovery scanner
# skips these wholesale, and ``deploy_runtime`` populates the cluster's
# ``.hpc/`` with framework artifacts.
RESERVED_DIRS = frozenset({".hpc"})

ALLOWED_CLUSTER_KEYS = frozenset(
    {
        "host",
        "user",
        "scheduler",
        "scratch",
        "modules",
        "conda_source",
        "conda_envs",
        "gpu_types",
        "constraints",
        "default_partition",
        # Present in current config/clusters.yaml; infra-shaped, so allow.
        "account",
        "gpu_constraint",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff_message(label: str, actual: set[str], expected: set[str]) -> str:
    """Format an actionable diff for failed allowlist comparisons."""
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    parts = [f"{label} drift detected. See {CONTRACT_DOC}."]
    if missing:
        parts.append(f"  Missing (declared in contract, absent from code): {missing}")
    if extra:
        parts.append(f"  Unexpected (present in code, absent from contract): {extra}")
    return "\n".join(parts)


def _walk_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` file under ``root`` (recursively)."""
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _imported_top_level_modules(path: Path) -> set[str]:
    """Parse ``path`` with ``ast`` and return the set of imported top-level modules.

    Walks the entire tree (not just module-level) so nested ``import`` statements
    inside functions or conditionals are also caught.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"Failed to parse {path}: {exc}") from exc

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (node.level > 0); they cannot reach across
            # the boundary by construction.
            if node.level == 0 and node.module:
                modules.add(node.module.split(".", 1)[0])
    return modules


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_public_api_matches_contract() -> None:
    """``hpc_mapreduce.__all__`` must match the allowlist exactly."""
    actual = set(hpc_mapreduce.__all__)
    expected = set(ALLOWED_EXPORTS)
    assert actual == expected, _diff_message(
        "hpc_mapreduce public API", actual, expected
    )


def test_reserved_filenames_match_contract() -> None:
    """``_SKIP_BASENAMES`` in discover.py must match the reserved-filenames allowlist."""
    actual = set(_SKIP_BASENAMES)
    expected = set(RESERVED_FILES)
    assert actual == expected, _diff_message(
        "Reserved filenames (_SKIP_BASENAMES)", actual, expected
    )


def test_reserved_dirs_match_contract() -> None:
    """``_SKIP_DIRS`` in discover.py must include the reserved-dirs allowlist."""
    # _SKIP_DIRS may include build/cache dirs (.git, __pycache__, .mypy_cache)
    # in addition to the framework-reserved .hpc; only require the latter is
    # present.
    actual = set(_SKIP_DIRS)
    missing = set(RESERVED_DIRS) - actual
    assert not missing, _diff_message(
        "Reserved dirs (_SKIP_DIRS)", actual, set(RESERVED_DIRS)
    )


def test_core_does_not_import_templates() -> None:
    """No file under ``hpc_mapreduce/`` may import from ``templates``."""
    core_root = REPO_ROOT / "hpc_mapreduce"
    offenders: list[str] = []
    for path in _walk_python_files(core_root):
        if "templates" in _imported_top_level_modules(path):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"hpc_mapreduce/** must not import from templates/. See {CONTRACT_DOC}.\n"
        f"  Offending files: {offenders}"
    )


# Submodules of ``hpc_mapreduce`` that are intentionally deployed alongside
# user executors on the cluster by ``deploy_runtime`` (see
# ``hpc_mapreduce/infra/remote.py``). Templates may import from these because
# they are guaranteed to be present at execution time on the compute node.
# Keep this list narrow; new entries require a matching update to
# ``docs/boundary-contract.md``.
RUNTIME_MODULES_ALLOWED_IN_TEMPLATES = frozenset(
    {
        "hpc_mapreduce.map.metrics_io",
    }
)


def _imported_dotted_modules(path: Path) -> set[str]:
    """Return the set of fully-qualified imported module names in ``path``.

    Like :func:`_imported_top_level_modules` but returns the full dotted name
    so callers can distinguish ``hpc_mapreduce.map.metrics_io`` (a deployed
    runtime module) from ``hpc_mapreduce.job.runs`` (framework-internal).
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"Failed to parse {path}: {exc}") from exc

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def test_templates_do_not_import_core() -> None:
    """No file under ``templates/`` may import from ``hpc_mapreduce``.

    Exception: a small allowlist of runtime modules deployed alongside the
    executor by ``deploy_runtime`` (see
    ``RUNTIME_MODULES_ALLOWED_IN_TEMPLATES``). New entries require a matching
    update to ``docs/boundary-contract.md``.
    """
    templates_root = REPO_ROOT / "templates"
    offenders: list[tuple[str, list[str]]] = []
    for path in _walk_python_files(templates_root):
        imported = _imported_dotted_modules(path)
        bad = sorted(
            m
            for m in imported
            if (m == "hpc_mapreduce" or m.startswith("hpc_mapreduce."))
            and m not in RUNTIME_MODULES_ALLOWED_IN_TEMPLATES
        )
        if bad:
            offenders.append((str(path.relative_to(REPO_ROOT)), bad))
    assert not offenders, (
        f"templates/** must not import from hpc_mapreduce (except deployed "
        f"runtime modules). See {CONTRACT_DOC}.\n"
        + "\n".join(f"  {p}: {mods}" for p, mods in offenders)
    )


def test_clusters_yaml_is_infra_only() -> None:
    """Each cluster entry in ``hpc_mapreduce/config/clusters.yaml`` must use only infra keys."""
    clusters_path = REPO_ROOT / "hpc_mapreduce" / "config" / "clusters.yaml"
    with clusters_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    assert isinstance(data, dict), (
        f"config/clusters.yaml must be a mapping of cluster_name -> config; got {type(data).__name__}."
    )

    violations: list[str] = []
    for cluster_name, cluster_cfg in data.items():
        if not isinstance(cluster_cfg, dict):
            violations.append(
                f"  {cluster_name!r}: expected mapping, got {type(cluster_cfg).__name__}"
            )
            continue
        unexpected = sorted(set(cluster_cfg.keys()) - ALLOWED_CLUSTER_KEYS)
        if unexpected:
            violations.append(f"  {cluster_name!r}: unexpected keys {unexpected}")

    assert not violations, (
        f"config/clusters.yaml contains non-infra keys. See {CONTRACT_DOC}.\n"
        + "\n".join(violations)
    )
