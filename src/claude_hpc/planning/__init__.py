"""claude_hpc.planning — submission orchestration on top of forecasts.

Submodules are deliberately importer-explicit. ``planning/planner.py``
imports the forecast layer; ``infra/clusters.py`` (loaded very early
because the package ``__init__`` reaches for it) imports
``planning.constraints`` — eager re-exports here would close that
cycle on first ``import claude_hpc``. Reach for the specific submodule:

* :mod:`claude_hpc.planning.planner` — main score-submit-plan entry point.
* :mod:`claude_hpc.planning.validate` — scheduler --test-only probe wrapper.
* :mod:`claude_hpc.planning.daisy_chain` — chain planning for walltime overflow.
* :mod:`claude_hpc.planning.resubmit_planner` — survival-atom application
  for resubmits.
"""
