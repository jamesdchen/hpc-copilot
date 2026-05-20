"""hpc_agent.planning — submission orchestration on top of forecasts.

Submodules are deliberately importer-explicit. ``planning/planner.py``
imports the forecast layer; ``infra/clusters.py`` (loaded very early
because the package ``__init__`` reaches for it) imports
``planning.constraints`` — eager re-exports here would close that
cycle on first ``import hpc_agent``. Reach for the specific submodule:

* :mod:`hpc_agent.planning.planner` — main score-submit-plan entry point.
* :mod:`hpc_agent.planning.validate` — scheduler --test-only probe wrapper.
* :mod:`hpc_agent.planning.daisy_chain` — chain planning for walltime overflow.
* :mod:`hpc_agent.planning.resubmit_planner` — survival-atom application
  for resubmits.
"""
