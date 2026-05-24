"""hpc_agent.planning — submission planning helpers.

Submodules are deliberately importer-explicit: ``infra/clusters.py``
(loaded very early because the package ``__init__`` reaches for it)
imports ``planning.constraints``, so eager re-exports here would close
that import cycle on first ``import hpc_agent``. Reach for the specific
submodule:

* :mod:`hpc_agent.planning.constraints` — cluster constraint parsing.
* :mod:`hpc_agent.planning.resubmit_batching` — pack failed task IDs
  into compact scheduler array expressions.
* :mod:`hpc_agent.ops.submit.throughput` — batch a task grid into waves
  (moved to ops/submit/ in the Wave 2 reorg).

Per-experiment on-disk state (``axes.yaml`` reader/writer, ``stages.py``
loader) lives in :mod:`hpc_agent.state.axes` / :mod:`hpc_agent.state.stages`
— planning/ is pure (no disk I/O, no importlib of user code); reading
or writing state belongs in state/.
"""
