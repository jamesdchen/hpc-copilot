"""Per-library solver adapters — checkpoint injection for library-owned loops.

The checkpoint helpers (:mod:`hpc_agent.experiment_kit.checkpoint`) assume the
executor owns its iteration loop: the loop body calls ``should_checkpoint()``
and ``write_checkpoint()`` (or hands the whole loop to ``run_iterations``).
A solver library like PETSc breaks that assumption — the time-stepping /
nonlinear-solve loop lives inside the library (``TSSolve`` is C code), so
there is no Python loop body to instrument and no AST the framework could
rewrite.

What such libraries *do* expose are injection hooks of their own: per-step
monitor callbacks and an options database settable from the environment. A
solver adapter maps the framework's checkpoint contract onto those hooks, so
checkpoint/resume can be injected the same two ways ``@register_run`` is
today:

* **Direct decoration** (a petsc4py script the user can edit): attach a
  monitor callback built by the adapter — the monitor body is exactly the
  existing ``should_checkpoint()`` + write pair, pointed at the library's
  native serialization.
* **Materialized wrapper** (an opaque ``main.py`` / compiled binary declared
  as a ``shell_command`` entry point): the wrapper exports the adapter's
  options-database fragment via the environment; the user's code stays
  untouched, exactly as :mod:`hpc_agent.incorporation.wrap_entry_point`
  promises.

Each adapter module also exports a ``detect_*`` AST matcher (same style as
:mod:`hpc_agent.experiment_kit.axis_matcher.matchers`) so onboarding flows
can recognize the library and offer instrumentation automatically.

Stdlib-only, like the rest of :mod:`hpc_agent.experiment_kit` — the solver
library itself is imported lazily inside runtime callables, never at module
import time, so these modules are safe to import at dispatch time on a
cluster runtime that does not have the library installed.

Adapters: :mod:`~hpc_agent.experiment_kit.solver_adapters.petsc` (PETSc /
petsc4py, TS + SNES).
"""

from __future__ import annotations

from hpc_agent.experiment_kit.solver_adapters.petsc import (
    PetscDetection,
    detect_petsc_solver,
    make_checkpoint_monitor,
)

__all__ = [
    "PetscDetection",
    "detect_petsc_solver",
    "make_checkpoint_monitor",
]
