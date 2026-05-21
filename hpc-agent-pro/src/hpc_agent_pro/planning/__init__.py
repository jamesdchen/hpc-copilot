"""hpc_agent_pro.planning — submission orchestration on top of forecasts.

Submodules are deliberately importer-explicit. Reach for the specific
submodule:

* :mod:`hpc_agent_pro.planning.planner` — main score-submit-plan entry point.
* :mod:`hpc_agent_pro.planning.validate` — scheduler --test-only probe wrapper.
* :mod:`hpc_agent_pro.planning.daisy_chain` — chain planning for walltime overflow.
* :mod:`hpc_agent_pro.planning.resubmit_planner` — survival-atom application
  for resubmits.
"""
