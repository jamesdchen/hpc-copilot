"""hpc-mapreduce: MapReduce-style HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
GPU selection, and experiment-agnostic grid dispatch. Cluster infrastructure
is configured via clusters.yaml; experiment setup is conversational.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("claude-hpc")
except PackageNotFoundError:  # pragma: no cover — running from a non-installed checkout
    __version__ = "0.0.0+unknown"

__all__ = [
    # Package root
    "_PACKAGE_ROOT",
    "__version__",
    # Config & discovery
    "load_clusters_config",
    "get_template_path",
    # Framework subdirectory layout
    "HPC_SUBDIR",
    "TASKS_FILENAME",
    "RUNS_SUBDIR",
    "framework_subdir",
    "runs_subdir",
    "tasks_path",
    "load_tasks_module",
    # Per-run sidecars
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
    "report_status",
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
    # Resubmit
    "compact_task_ids",
    "ResubmitBatch",
    "ResubmitPlan",
    "resubmit_plan",
    # Remote
    "run_combiner",
    "run_combiner_checked",
    # Per-task metrics sidecar
    "write_metrics",
]

import importlib.util
from pathlib import Path
from types import ModuleType

from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.infra.gpu import pick_gpu
from hpc_mapreduce.infra.remote import (
    deploy_runtime,
    rsync_pull,
    rsync_push,
    run_combiner,
    run_combiner_checked,
    ssh_run,
)
from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints
from hpc_mapreduce.job.discover import (
    ExecutorInfo,
    discover_executors,
    is_executor_source,
)
from hpc_mapreduce.job.resubmit import (
    ResubmitBatch,
    ResubmitPlan,
    compact_task_ids,
    resubmit_plan,
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
    SubmissionPlan,
    WorkloadSpec,
    build_wave_map,
    compute_submission_plan,
)
from hpc_mapreduce.map.metrics_io import write_metrics
from hpc_mapreduce.reduce.classify import classify_failure
from hpc_mapreduce.reduce.metrics import (
    reduce_by_grid_point,
    reduce_metrics,
    reduce_partials,
    reduce_resource_usage,
)
from hpc_mapreduce.reduce.status import (
    check_results,
    detect_scheduler,
    report_status,
    rollup_by_grid_point,
)

_PACKAGE_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Framework subdirectory layout (.hpc/)
# ---------------------------------------------------------------------------

HPC_SUBDIR: str = ".hpc"
TASKS_FILENAME: str = "tasks.py"
RUNS_SUBDIR: str = "runs"


def framework_subdir(experiment_dir: Path) -> Path:
    """Return ``experiment_dir/.hpc``, creating it idempotently.

    Also writes ``.hpc/.gitignore`` (ignoring ``runs/``) on first call so
    per-run sidecars don't pollute the user's git history while
    ``tasks.py`` stays tracked.
    """
    sub = Path(experiment_dir) / HPC_SUBDIR
    sub.mkdir(parents=True, exist_ok=True)
    gitignore = sub / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(f"{RUNS_SUBDIR}/\n")
    return sub


def runs_subdir(experiment_dir: Path) -> Path:
    """Return ``experiment_dir/.hpc/runs``, creating it idempotently."""
    sub = framework_subdir(experiment_dir) / RUNS_SUBDIR
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def tasks_path(experiment_dir: Path) -> Path:
    """Return ``experiment_dir/.hpc/tasks.py`` (does not create the file)."""
    return Path(experiment_dir) / HPC_SUBDIR / TASKS_FILENAME


def load_tasks_module(tasks_py_path: Path) -> ModuleType:
    """Import a user's ``tasks.py`` from an arbitrary path via importlib.

    The returned module must expose ``total()`` and ``resolve(task_id)``.
    Callers should treat any ``AttributeError``, ``TypeError``, or
    ``ImportError`` from the user's code as a submit-time error worth
    surfacing, not a framework bug.
    """
    path = Path(tasks_py_path)
    if not path.is_file():
        raise FileNotFoundError(f"tasks.py not found: {path}")
    spec = importlib.util.spec_from_file_location("hpc_user_tasks", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load tasks.py from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "total") or not hasattr(module, "resolve"):
        raise AttributeError(
            f"{path} must define both total() and resolve(task_id) — "
            "see hpc_mapreduce/templates/tasks_example.py"
        )
    return module


def get_template_path(scheduler: str, template: str) -> Path:
    """Return the absolute path to a job template shipped with claude-hpc.

    Parameters
    ----------
    scheduler : ``"sge"`` or ``"slurm"``
    template : template name without extension (e.g. ``"cpu_array"``, ``"gpu_array"``)

    Returns
    -------
    Path to the template file.

    Raises
    ------
    FileNotFoundError
        If the resolved template does not exist on disk.
    """
    ext = ".sh" if scheduler == "sge" else ".slurm"
    path = Path(__file__).parent / "templates" / scheduler / f"{template}{ext}"
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path
