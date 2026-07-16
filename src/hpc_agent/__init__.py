"""hpc-agent: MapReduce-style HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
GPU selection, and array-batch dispatch driven by a user-written
``.hpc/tasks.py``. Cluster infrastructure is configured via
``clusters.yaml``; experiment setup is conversational.
"""

import importlib
import importlib.util
import warnings
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

# Static-checker mirror of the runtime-lazy surface (B3 / PEP 562). At runtime
# ``JournalLayout``, ``load_clusters_config``, the primitive-registry symbols
# and ``register_run`` are resolved on first access by ``__getattr__`` from the
# ``_LAZY_PUBLIC`` table below — importing them eagerly here would drag
# ``hpc_agent.infra`` (pydantic + yaml + transport, ~0.5s) and the kernel
# registry into every ``import hpc_agent``, the cold-start tax B3 removes. mypy
# cannot see through a module ``__getattr__``, so this ``TYPE_CHECKING`` block
# gives it the real types for ``hpc_agent.<name>`` attribute access. It is a
# load-bearing twin of ``_LAZY_PUBLIC``: the annotation below names the pinning
# test that reds if the two drift.
# MIRROR: hpc_agent.__init__::_LAZY_PUBLIC <-> this TYPE_CHECKING import block
#   pinned-by tests/contracts/test_eager_import_smoke.py::test_type_checking_mirror
if TYPE_CHECKING:
    from hpc_agent._kernel.contract.layout import JournalLayout, RepoLayout
    from hpc_agent._kernel.registry.primitive import (
        PrimitiveMeta,
        SideEffect,
        get_meta,
        get_registry,
        primitive,
        register_primitives,
    )
    from hpc_agent.experiment_kit import register_run
    from hpc_agent.infra.clusters import load_clusters_config

# Package root. Cheap (``Path.resolve`` on ``__file__``) and read at module
# scope by consumers (e.g. ``incorporation/build/executor.py``,
# ``incorporation/build/tasks_py.py`` do ``from hpc_agent import _PACKAGE_ROOT``
# during their own import), so it stays eager — never deferred to
# ``__getattr__`` (G4: honest underscore-attr resolution, no import-time work).
_PACKAGE_ROOT = Path(__file__).resolve().parent

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
    # Researcher-facing experiment API
    "register_run",
]

# Canonical researcher-facing symbols whose implementation lives in a
# submodule but which ARE part of the documented root API (see
# docs/reference/boundary-contract.md) — the ``@register_run`` decorator
# users put on their experiment functions; ``hpc-wrap-entry-point``'s
# SKILL.md documents ``from hpc_agent import register_run``. Resolved
# lazily through ``__getattr__`` to dodge an import cycle: experiment_kit
# submodules do ``from hpc_agent import errors`` at module load, so
# eager-importing register_run at the top of this module would deadlock.
# Unlike ``_MOVED``, no ``DeprecationWarning`` — this IS the current home.
_LAZY_PUBLIC: dict[str, str] = {
    "register_run": "hpc_agent.experiment_kit.register_run",
    # B3 (PEP 562): the documented-root symbols whose eager import used to
    # drag ``hpc_agent.infra`` (pydantic/yaml/transport) and the kernel
    # registry into every ``import hpc_agent``. Resolved on first access.
    # These ARE the current home (no DeprecationWarning) — unlike ``_MOVED``.
    "JournalLayout": "hpc_agent._kernel.contract.layout.JournalLayout",
    "RepoLayout": "hpc_agent._kernel.contract.layout.RepoLayout",
    "PrimitiveMeta": "hpc_agent._kernel.registry.primitive.PrimitiveMeta",
    "SideEffect": "hpc_agent._kernel.registry.primitive.SideEffect",
    "get_meta": "hpc_agent._kernel.registry.primitive.get_meta",
    "get_registry": "hpc_agent._kernel.registry.primitive.get_registry",
    "primitive": "hpc_agent._kernel.registry.primitive.primitive",
    "register_primitives": "hpc_agent._kernel.registry.primitive.register_primitives",
    "load_clusters_config": "hpc_agent.infra.clusters.load_clusters_config",
}


def _resolve_version() -> str:
    """Look up the installed distribution version, lazily.

    ``importlib.metadata`` pulls in ``email`` / ``zipfile`` (~0.15s cold), so
    reading it eagerly at module load taxed every ``import hpc_agent``. Deferred
    to first access of ``hpc_agent.__version__`` via ``__getattr__``; the result
    is cached back into module globals so subsequent reads are free.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        return _pkg_version("hpc-agent")
    except PackageNotFoundError:  # pragma: no cover — running from a non-installed checkout
        return "0.0.0+unknown"


# Names moved out of the root namespace. The ``__getattr__`` shim below
# resolves each one with a ``DeprecationWarning`` for one release;
# external callers should switch to the canonical home. Keep this
# table in sync with the move table in CHANGELOG.md + the
# ``ALLOWED_EXPORTS`` allowlist in
# ``tests/contracts/test_boundary_contract.py``.
_MOVED: dict[str, str] = {
    # Per-run sidecars → hpc_agent.state.runs / .run_sha
    "MAX_RUNS": "hpc_agent.state.runs.MAX_RUNS",
    "SIDECAR_SCHEMA_VERSION": "hpc_agent.state.runs.SIDECAR_SCHEMA_VERSION",
    "compute_cmd_sha": "hpc_agent.state.run_sha.compute_cmd_sha",
    "compute_tasks_py_sha": "hpc_agent.state.run_sha.compute_tasks_py_sha",
    "find_existing_runs": "hpc_agent.state.runs.find_existing_runs",
    "find_run_by_cmd_sha": "hpc_agent.state.runs.find_run_by_cmd_sha",
    "prune_old_runs": "hpc_agent.state.runs.prune_old_runs",
    "read_run_sidecar": "hpc_agent.state.runs.read_run_sidecar",
    "run_sidecar_path": "hpc_agent.state.runs.run_sidecar_path",
    "write_run_sidecar": "hpc_agent.state.runs.write_run_sidecar",
    # Remote execution → hpc_agent.infra.remote / .transport
    "deploy_runtime": "hpc_agent.infra.transport.deploy_runtime",
    "rsync_pull": "hpc_agent.infra.transport.rsync_pull",
    "rsync_push": "hpc_agent.infra.transport.rsync_push",
    "run_combiner": "hpc_agent.infra.transport.run_combiner",
    "run_combiner_checked": "hpc_agent.infra.transport.run_combiner_checked",
    "ssh_run": "hpc_agent.infra.remote.ssh_run",
    # Job status & results → hpc_agent.execution.mapreduce.reduce.status
    "check_results": "hpc_agent.execution.mapreduce.reduce.status.check_results",
    "check_results_from_tasks": (
        "hpc_agent.execution.mapreduce.reduce.status.check_results_from_tasks"
    ),
    "detect_scheduler": "hpc_agent.execution.mapreduce.reduce.status.detect_scheduler",
    "report_status": "hpc_agent.execution.mapreduce.reduce.status.report_status",
    "report_status_from_tasks": (
        "hpc_agent.execution.mapreduce.reduce.status.report_status_from_tasks"
    ),
    "rollup_by_grid_point": "hpc_agent.execution.mapreduce.reduce.status.rollup_by_grid_point",
    # GPU selection
    "pick_gpu": "hpc_agent.infra.gpu.pick_gpu",
    # Reduce
    "classify_failure": "hpc_agent.execution.mapreduce.reduce.classify.classify_failure",
    "reduce_by_grid_point": "hpc_agent.execution.mapreduce.reduce.metrics.reduce_by_grid_point",
    "reduce_metrics": "hpc_agent.execution.mapreduce.reduce.metrics.reduce_metrics",
    "reduce_partials": "hpc_agent.execution.mapreduce.reduce.metrics.reduce_partials",
    "reduce_resource_usage": "hpc_agent.execution.mapreduce.reduce.metrics.reduce_resource_usage",
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
    "write_metrics": "hpc_agent.execution.mapreduce.metrics_io.write_metrics",
}


def __getattr__(name: str) -> Any:
    """Resolve a lazily-deferred, moved-out, or version attribute (PEP 562).

    Three cases, in order:

    * :data:`_LAZY_PUBLIC` — current-home root symbols resolved on first
      access (B3 defers their heavy imports out of ``import hpc_agent``); no
      warning, this IS the canonical home.
    * ``__version__`` — computed by :func:`_resolve_version` and cached back
      into module globals so later reads skip ``importlib.metadata``.
    * :data:`_MOVED` — historical attributes that left the root (item 6
      trimmed :data:`__all__` to the boundary-contract surface); still
      importable for one release with a :class:`DeprecationWarning` pointing
      at the canonical module path. Drop the shim in a future release.

    Any other name — including an unknown underscore attribute (G4) — raises
    an honest :class:`AttributeError`; a broken target path surfaces the
    underlying :class:`ImportError`/:class:`AttributeError` unswallowed.
    """
    public_target = _LAZY_PUBLIC.get(name)
    if public_target is not None:
        module_path, _, attr = public_target.rpartition(".")
        return getattr(importlib.import_module(module_path), attr)
    if name == "__version__":
        version = _resolve_version()
        globals()["__version__"] = version
        return version
    target = _MOVED.get(name)
    if target is None:
        raise AttributeError(f"module 'hpc_agent' has no attribute {name!r}")
    warnings.warn(
        f"{name!r} moved out of the hpc_agent root namespace; import from {target} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    module_path, _, attr = target.rpartition(".")
    return getattr(importlib.import_module(module_path), attr)


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
    # Put the experiment root and ``.hpc/`` on ``sys.path`` for the duration
    # of ``exec_module`` so a tasks.py that does ``import my_root_module`` or
    # ``from src.x import y`` (where those live at the experiment root) resolves
    # during LOCAL enumeration (compute-run-id / build-submit-spec) exactly as
    # it does on the CLUSTER. The cluster job script exports
    # ``PYTHONPATH="$REPO_DIR:$REPO_DIR/.hpc"`` (see
    # execution/mapreduce/templates/runtime/common/hpc_preamble.sh) before the
    # dispatcher imports tasks.py; without this, the same tasks.py imports fine
    # on the cluster but raises ModuleNotFoundError locally. Layout is
    # ``<experiment_dir>/.hpc/tasks.py`` (see RepoLayout), so the experiment
    # root is ``path.parent.parent`` and ``.hpc/`` is ``path.parent``. Mirror
    # the cluster ordering (root before ``.hpc/``); skip any entry already on
    # the path to avoid duplicate insertions, and restore the original
    # ``sys.path`` afterward so we don't leak entries into the host process.
    _exp_root = str(path.resolve().parent.parent)
    _hpc_dir = str(path.resolve().parent)
    _saved_sys_path = list(_sys.path)
    for _entry in (_hpc_dir, _exp_root):
        if _entry not in _sys.path:
            _sys.path.insert(0, _entry)
    try:
        spec.loader.exec_module(module)
    finally:
        _sys.path[:] = _saved_sys_path
    if not hasattr(module, "total") or not hasattr(module, "resolve"):
        raise AttributeError(
            f"{path} must define both total() and resolve(task_id) — "
            "see hpc_agent/execution/mapreduce/templates/scaffolds/tasks_example.py"
        )
    return module


def get_template_path(scheduler: str, template: str) -> Path:
    """Deprecated. Materialise a rendered job script and return its path.

    .. deprecated::
        The runtime array scripts are no longer static files on disk —
        they are *rendered* from the scheduler profile (Phase 2 / Option
        C). Prefer the text directly::

            from hpc_agent.infra.backends import get_backend_class
            body = get_backend_class(scheduler).render_script(kind="cpu")

        This shim is retained for back-compat: it renders the script and
        writes it to a stable per-(scheduler, template) path under the temp
        dir (overwritten in place — no unbounded accumulation), then
        returns that path.

    Parameters
    ----------
    scheduler : ``"sge"``, ``"slurm"``, ``"pbspro"`` or ``"torque"``
    template : template basename (e.g. ``"cpu_array"`` / ``"gpu_array"``);
        the ``_array`` suffix maps to the profile script ``kind``.
    """
    import tempfile
    import warnings

    warnings.warn(
        "hpc_agent.get_template_path is deprecated; the runtime array scripts "
        "are rendered from the scheduler profile. Use "
        "hpc_agent.infra.backends.get_backend_class(scheduler).render_script("
        'kind="cpu"|"gpu") instead.',
        DeprecationWarning,
        stacklevel=2,
    )

    from hpc_agent.infra.backends import get_backend_class, template_ext_for

    backend_cls = get_backend_class(scheduler)
    ext = template_ext_for(scheduler)
    kind = template.replace("_array", "")  # "cpu_array" -> "cpu"
    rendered = backend_cls.render_script(kind=kind)

    cache_dir = Path(tempfile.gettempdir()) / "hpc_agent_templates" / scheduler
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{template}{ext}"
    path.write_text(rendered, encoding="utf-8", newline="")
    return path
