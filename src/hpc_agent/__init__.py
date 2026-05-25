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
    # Path resolution — canonical home for the .hpc/ layout
    "JournalLayout",
    "RepoLayout",
    # Config & discovery
    "get_template_path",
    "load_clusters_config",
    # Framework subdirectory layout (the .hpc/tasks.py model)
    "RUNS_SUBDIR",
    "TASKS_FILENAME",
    "load_tasks_module",
    # Primitive registry — the agent-extension surface
    "PrimitiveMeta",
    "SideEffect",
    "get_meta",
    "get_registry",
    "primitive",
    "register_primitives",
]

# Names moved out of the root namespace. The ``__getattr__`` shim below
# resolves each one with a ``DeprecationWarning`` for one release;
# external callers should switch to the canonical home. Keep this
# table in sync with the move table in CHANGELOG.md + the
# ``ALLOWED_EXPORTS`` allowlist in
# ``tests/contracts/test_boundary_contract.py``.
_MOVED: dict[str, str] = {
    # Per-run sidecars → hpc_agent.state.runs
    "MAX_RUNS": "hpc_agent.state.runs.MAX_RUNS",
    "SIDECAR_SCHEMA_VERSION": "hpc_agent.state.runs.SIDECAR_SCHEMA_VERSION",
    "compute_cmd_sha": "hpc_agent.state.runs.compute_cmd_sha",
    "compute_tasks_py_sha": "hpc_agent.state.runs.compute_tasks_py_sha",
    "find_existing_runs": "hpc_agent.state.runs.find_existing_runs",
    "find_run_by_cmd_sha": "hpc_agent.state.runs.find_run_by_cmd_sha",
    "prune_old_runs": "hpc_agent.state.runs.prune_old_runs",
    "read_run_sidecar": "hpc_agent.state.runs.read_run_sidecar",
    "run_sidecar_path": "hpc_agent.state.runs.run_sidecar_path",
    "write_run_sidecar": "hpc_agent.state.runs.write_run_sidecar",
    # Remote execution → hpc_agent.infra.remote
    "deploy_runtime": "hpc_agent.infra.remote.deploy_runtime",
    "rsync_pull": "hpc_agent.infra.remote.rsync_pull",
    "rsync_push": "hpc_agent.infra.remote.rsync_push",
    "run_combiner": "hpc_agent.infra.remote.run_combiner",
    "run_combiner_checked": "hpc_agent.infra.remote.run_combiner_checked",
    "ssh_run": "hpc_agent.infra.remote.ssh_run",
    # Job status & results → hpc_agent.models.mapreduce.reduce.status
    "check_results": "hpc_agent.models.mapreduce.reduce.status.check_results",
    "check_results_from_tasks": "hpc_agent.models.mapreduce.reduce.status.check_results_from_tasks",
    "detect_scheduler": "hpc_agent.models.mapreduce.reduce.status.detect_scheduler",
    "report_status": "hpc_agent.models.mapreduce.reduce.status.report_status",
    "report_status_from_tasks": "hpc_agent.models.mapreduce.reduce.status.report_status_from_tasks",
    "rollup_by_grid_point": "hpc_agent.models.mapreduce.reduce.status.rollup_by_grid_point",
    # GPU selection
    "pick_gpu": "hpc_agent.infra.gpu.pick_gpu",
    # Reduce
    "classify_failure": "hpc_agent.models.mapreduce.reduce.classify.classify_failure",
    "reduce_by_grid_point": "hpc_agent.models.mapreduce.reduce.metrics.reduce_by_grid_point",
    "reduce_metrics": "hpc_agent.models.mapreduce.reduce.metrics.reduce_metrics",
    "reduce_partials": "hpc_agent.models.mapreduce.reduce.metrics.reduce_partials",
    "reduce_resource_usage": "hpc_agent.models.mapreduce.reduce.metrics.reduce_resource_usage",
    # Executor discovery
    "ExecutorInfo": "hpc_agent.state.discover.ExecutorInfo",
    "discover_executors": "hpc_agent.state.discover.discover_executors",
    "is_executor_source": "hpc_agent.state.discover.is_executor_source",
    # Cluster constraints
    "ClusterConstraints": "hpc_agent.infra.constraints.ClusterConstraints",
    "parse_constraints": "hpc_agent.infra.constraints.parse_constraints",
    # Throughput optimizer
    "SubmissionPlan": "hpc_agent.infra.throughput.SubmissionPlan",
    "WorkloadSpec": "hpc_agent.infra.throughput.WorkloadSpec",
    "build_wave_map": "hpc_agent.infra.throughput.build_wave_map",
    "compute_submission_plan": "hpc_agent.infra.throughput.compute_submission_plan",
    # Smart-submit data layer
    "append_runtime_sample": "hpc_agent.state.runtime_prior.append_sample",
    "inspect_cluster": "hpc_agent.infra.inspect.inspect_cluster",
    "roll_up_runtime_quantiles": "hpc_agent.state.runtime_prior.roll_up_quantiles",
    # Resubmit
    "ResubmitBatch": "hpc_agent.ops.recover.batching.ResubmitBatch",
    "ResubmitPlan": "hpc_agent.ops.recover.batching.ResubmitPlan",
    "compact_task_ids": "hpc_agent.ops.recover.batching.compact_task_ids",
    "resubmit_plan": "hpc_agent.ops.recover.batching.resubmit_plan",
    # Per-task metrics sidecar
    "write_metrics": "hpc_agent.models.mapreduce.metrics_io.write_metrics",
}

import importlib
import importlib.util
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

from hpc_agent._kernel.contract.layout import JournalLayout, RepoLayout
from hpc_agent._kernel.registry.primitive import (
    PrimitiveMeta,
    SideEffect,
    get_meta,
    get_registry,
    primitive,
    register_primitives,
)
from hpc_agent.infra.clusters import load_clusters_config


def __getattr__(name: str) -> Any:
    """Resolve a moved-out name from its canonical home.

    Item 6 trimmed the root :data:`__all__` to the integrator surface
    documented in ``docs/reference/boundary-contract.md``. The 37
    historical attributes that left the root are listed in
    :data:`_MOVED`; importing one through ``hpc_agent.<name>`` (or
    ``from hpc_agent import <name>``) still works for one release but
    emits a :class:`DeprecationWarning` pointing the caller at the
    canonical module path. Drop the shim in a future release.
    """
    target = _MOVED.get(name)
    if target is None:
        raise AttributeError(f"module 'hpc_agent' has no attribute {name!r}")
    warnings.warn(
        f"{name!r} moved out of the hpc_agent root namespace; " f"import from {target} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    module_path, _, attr = target.rpartition(".")
    return getattr(importlib.import_module(module_path), attr)


_PACKAGE_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Framework subdirectory layout (.hpc/)
#
# Canonical home: :class:`hpc_agent._kernel.contract.layout.RepoLayout`.
# Use ``RepoLayout(experiment_dir).hpc`` / ``.runs`` / ``.tasks``
# directly. The 0.2.0-vintage forwarders ``framework_subdir``,
# ``runs_subdir``, and ``tasks_path`` were removed in 0.5.0 (the 0.4.0
# cut had missed them despite the deprecation note); any external
# caller still importing them by name should switch to RepoLayout.
# ---------------------------------------------------------------------------

TASKS_FILENAME: str = "tasks.py"
RUNS_SUBDIR: str = "runs"


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
            "see hpc_agent/models/mapreduce/templates/scaffolds/tasks_example.py"
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
    # B7: templates moved to hpc_agent/models/mapreduce/templates/ as part
    # of the package reorg. Resolve via the hpc_agent package root so this
    # forwarder keeps working until the rest of __init__.py moves over.
    import hpc_agent as _hpc_agent_pkg

    _hpc_agent_root = Path(_hpc_agent_pkg.__file__).resolve().parent
    path = (
        _hpc_agent_root
        / "models"
        / "mapreduce"
        / "templates"
        / "runtime"
        / scheduler
        / f"{template}{ext}"
    )
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path
