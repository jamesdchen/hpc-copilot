"""hpc-agent: MapReduce-style HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
GPU selection, and array-batch dispatch driven by a user-written
``.hpc/tasks.py``. Cluster infrastructure is configured via
``clusters.yaml``; experiment setup is conversational.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("hpc-agent")
except PackageNotFoundError:  # pragma: no cover — running from a non-installed checkout
    __version__ = "0.0.0+unknown"

__all__ = [
    # Package root
    "_PACKAGE_ROOT",
    "__version__",
    # Config & discovery
    "load_clusters_config",
    "get_template_path",
    # Framework subdirectory layout (NEW — the .hpc/tasks.py model)
    "TASKS_FILENAME",
    "RUNS_SUBDIR",
    "framework_subdir",
    "runs_subdir",
    "tasks_path",
    "load_tasks_module",
    # Path resolution (B1) — canonical home for the three forwarders above
    "RepoLayout",
    "JournalLayout",
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
    "append_runtime_sample",
    "roll_up_runtime_quantiles",
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
    # Primitive registry (C′ — implementation + schemas as SoT)
    "PrimitiveMeta",
    "SideEffect",
    "get_meta",
    "get_registry",
    "primitive",
    "register_primitives",
]

import importlib.util
from pathlib import Path
from types import ModuleType

from hpc_agent._internal.layout import JournalLayout, RepoLayout
from hpc_agent._internal.primitive import (
    PrimitiveMeta,
    SideEffect,
    get_meta,
    get_registry,
    primitive,
    register_primitives,
)
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.infra.gpu import pick_gpu
from hpc_agent.infra.inspect import inspect_cluster
from hpc_agent.infra.remote import (
    deploy_runtime,
    rsync_pull,
    rsync_push,
    run_combiner,
    run_combiner_checked,
    ssh_run,
)
from hpc_agent.mapreduce.metrics_io import write_metrics
from hpc_agent.mapreduce.reduce.classify import classify_failure
from hpc_agent.mapreduce.reduce.metrics import (
    reduce_by_grid_point,
    reduce_metrics,
    reduce_partials,
    reduce_resource_usage,
)
from hpc_agent.mapreduce.reduce.status import (
    check_results,
    check_results_from_tasks,
    detect_scheduler,
    report_status,
    report_status_from_tasks,
    rollup_by_grid_point,
)
from hpc_agent.ops.recover.batching import (
    ResubmitBatch,
    ResubmitPlan,
    compact_task_ids,
    resubmit_plan,
)
from hpc_agent.planning.constraints import ClusterConstraints, parse_constraints
from hpc_agent.planning.throughput import (
    SubmissionPlan,
    WorkloadSpec,
    build_wave_map,
    compute_submission_plan,
)
from hpc_agent.state.discover import (
    ExecutorInfo,
    discover_executors,
    is_executor_source,
)
from hpc_agent.state.runs import (
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
from hpc_agent.state.runtime_prior import append_sample as append_runtime_sample
from hpc_agent.state.runtime_prior import roll_up_quantiles as roll_up_runtime_quantiles

_PACKAGE_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Framework subdirectory layout (.hpc/)
#
# Canonical home: :class:`hpc_agent._internal.layout.RepoLayout`. The three
# functions below are back-compat forwarders kept so external callers /
# slash commands that imported them by name continue to work. New code
# should prefer ``RepoLayout(experiment_dir).hpc`` / ``.runs`` /
# ``.tasks`` directly.
#
# back-compat: introduced 0.2.0 (RepoLayout split). Remove in 0.4.0 —
# audit external callers via search; slash_commands/ is in-tree.
# ---------------------------------------------------------------------------

TASKS_FILENAME: str = "tasks.py"
RUNS_SUBDIR: str = "runs"


def framework_subdir(experiment_dir: Path) -> Path:
    """Deprecated forwarder for ``RepoLayout(experiment_dir).hpc``.

    Returns ``experiment_dir/.hpc``, creating it idempotently and
    writing ``.hpc/.gitignore`` on first call.
    """
    return RepoLayout(experiment_dir).hpc


def runs_subdir(experiment_dir: Path) -> Path:
    """Deprecated forwarder for ``RepoLayout(experiment_dir).runs``.

    Note: this is the *cluster sidecar* runs directory under
    ``<experiment_dir>/.hpc/runs/``, NOT the journal runs directory
    under ``~/.claude/hpc/<repo_hash>/runs/`` — that one is
    :attr:`JournalLayout.runs`. The pre-B1 name collision was a P0 bug
    source; ``RepoLayout`` / ``JournalLayout`` make it a type error.
    """
    return RepoLayout(experiment_dir).runs


def tasks_path(experiment_dir: Path) -> Path:
    """Deprecated forwarder for ``RepoLayout(experiment_dir).tasks``."""
    return RepoLayout(experiment_dir).tasks


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
    # Per-path unique module name so two different tasks.py files loaded
    # in the same Python process don't collide in the import cache (and
    # don't shadow any unrelated third-party module named
    # ``hpc_user_tasks``).
    import hashlib as _hashlib
    import sys as _sys

    _digest = _hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
    mod_name = f"hpc_user_tasks_{_digest}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load tasks.py from {path}")
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so:
    #   * pickle can re-locate classes defined inside tasks.py via
    #     ``sys.modules[cls.__module__]`` (otherwise pickle.loads raises
    #     ModuleNotFoundError in any subprocess worker that re-imports);
    #   * ``typing.get_type_hints`` can resolve forward references via
    #     ``sys.modules[cls.__module__].__dict__``;
    #   * any ``from __future__ import annotations`` user module that
    #     introspects its own type hints works.
    _sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "total") or not hasattr(module, "resolve"):
        raise AttributeError(
            f"{path} must define both total() and resolve(task_id) — "
            "see hpc_agent/mapreduce/templates/scaffolds/tasks_example.py"
        )
    return module


def get_template_path(scheduler: str, template: str) -> Path:
    """Return the absolute path to a job template shipped with hpc-agent.

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
    # B5-PR2: route through the backend registry instead of an inline
    # ladder. ``template_ext`` is a class attribute on each backend
    # (".sh" for SGE, ".slurm" for SLURM); this keeps the on-disk layout
    # under the backend's authority.
    from hpc_agent.infra.backends import template_ext_for

    ext = template_ext_for(scheduler)
    # B7: templates moved to hpc_agent/mapreduce/templates/ as part of
    # the package reorg. Resolve via the hpc_agent package root so this
    # forwarder keeps working until the rest of __init__.py moves over.
    import hpc_agent as _hpc_agent_pkg

    _hpc_agent_root = Path(_hpc_agent_pkg.__file__).resolve().parent
    path = _hpc_agent_root / "mapreduce" / "templates" / scheduler / f"{template}{ext}"
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path
