"""hpc_agent.planning — submission planning helpers.

Submodules are deliberately importer-explicit: ``infra/clusters.py``
(loaded very early because the package ``__init__`` reaches for it)
imports ``planning.constraints``, so eager re-exports here would close
that import cycle on first ``import hpc_agent``. Reach for the specific
submodule:

* :mod:`hpc_agent.planning.constraints` — cluster constraint parsing.
* :mod:`hpc_agent.planning.resubmit_batching` — pack failed task IDs
  into compact scheduler array expressions.
* :mod:`hpc_agent.planning.throughput` — batch a task grid into waves.
* :mod:`hpc_agent.planning.axes` — campaign axis-sweep helpers.
* :mod:`hpc_agent.planning.stages` — campaign stage loading.
"""
