"""``hpc_agent.experiment_kit`` — bring-a-notebook experiment + parallelization layer.

hpc-agent's core stays experiment-agnostic: the integrator hand-writes
``.hpc/tasks.py`` and owns every parameter-shape decision. This package
adds an *optional* layer on top so a researcher can hand the framework a
notebook (or a plain ``run()`` function) and have hpc-agent — not the
experiment repo — own parallelization.

Two layers, both stdlib-only so anything here is safe to import at
dispatch time on a stdlib-only cluster runtime:

**Layer 1 — notebook / CLI helpers**

- :func:`register_run` — decorator that marks an experiment entry point,
  synthesises its CLI :class:`~hpc_agent.executor_cli.Flag` list from the
  function signature, and injects a ``compute(args)`` wrapper into the
  module (satisfying the executor contract).
- :func:`save_artifact` — write a large artifact under the per-task
  output directory.
- :func:`export_notebook` — extract the importable surface of a
  ``.ipynb`` into a ``.py`` executor module (strict AST allowlist).
- :func:`discover_runs` — find ``@register_run`` functions by AST walk,
  without importing the experiment's heavy dependencies.
- :func:`flags_from_signature` / :func:`flags_for_run` — the
  signature → Flag mapping.

**Layer 2 — parallelization planner**

Parallelizing a computation is partitioning a totally-ordered series; it
is fungible with a serial run iff the partition does not cut an
unaccounted data dependency. One question classifies every axis: is
there carried state, and is the state transition associative?

- :class:`Independent` / :class:`Associative` / :class:`BoundedHalo` /
  :class:`Sequential` — the four :data:`DataAxis` cases.
- :func:`plan_tasks` — apply the strategy for a ``DataAxis`` and return a
  ``total()`` / ``resolve()`` object for ``.hpc/tasks.py``.
- :func:`load_series` — the halo-aware loader: the single seam that lets
  the framework hand each task its slice without the experiment knowing.
- :func:`check_elision` / :func:`assert_elision_equivalent` — the
  serial-elision harness: run an experiment once whole and once split,
  assert equality. This is the backstop that makes automated inference
  safe.
"""

from __future__ import annotations

from hpc_agent.experiment_kit.axis import (
    MOMENTS,
    SUM,
    Associative,
    BoundedHalo,
    DataAxis,
    Independent,
    Moments,
    Monoid,
    Sequential,
)
from hpc_agent.experiment_kit.axis_config import (
    HaloExprError,
    config_from_data_axis,
    data_axis_from_config,
    eval_halo_expr,
)
from hpc_agent.experiment_kit.discover import RunInfo, discover_runs
from hpc_agent.experiment_kit.elision import (
    ElisionReport,
    assert_elision_equivalent,
    check_elision,
)
from hpc_agent.experiment_kit.notebook import (
    export_notebook,
    export_notebook_markers,
    notebook_imports_runtime,
)
from hpc_agent.experiment_kit.plan import TaskPlan, plan_tasks, sweep_grid
from hpc_agent.experiment_kit.reduce import reduce_monoid, reduce_monoid_sidecars
from hpc_agent.experiment_kit.register import RunSpec, register_run, save_artifact
from hpc_agent.experiment_kit.series import (
    SeriesNotConfigured,
    SliceSpec,
    current_slice,
    load_series,
    set_series_loader,
    trim_emission,
)
from hpc_agent.experiment_kit.signature import (
    flags_for_run,
    flags_from_ast,
    flags_from_signature,
)

__all__ = [
    # Layer 1 — notebook / CLI helpers
    "register_run",
    "RunSpec",
    "save_artifact",
    "export_notebook",
    "export_notebook_markers",
    "notebook_imports_runtime",
    "discover_runs",
    "RunInfo",
    "flags_from_signature",
    "flags_from_ast",
    "flags_for_run",
    # Layer 2 — parallelization planner
    "DataAxis",
    "Independent",
    "Associative",
    "BoundedHalo",
    "Sequential",
    "Monoid",
    "Moments",
    "SUM",
    "MOMENTS",
    "plan_tasks",
    "TaskPlan",
    "sweep_grid",
    "data_axis_from_config",
    "config_from_data_axis",
    "eval_halo_expr",
    "HaloExprError",
    "load_series",
    "set_series_loader",
    "current_slice",
    "trim_emission",
    "SliceSpec",
    "SeriesNotConfigured",
    "reduce_monoid",
    "reduce_monoid_sidecars",
    "check_elision",
    "assert_elision_equivalent",
    "ElisionReport",
]
