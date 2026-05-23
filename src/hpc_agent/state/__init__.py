"""hpc_agent.state — persistent experiment-side metadata.

Read-only views over what previous runs produced (sidecars, runtime
priors, executor inventory) plus the per-experiment scheduling configs
(axes.yaml, stages.py). Submodules are deliberately importer-explicit;
reach for the specific submodule:

* :mod:`hpc_agent.state.runs` — per-run sidecars (``read_run_sidecar``,
  ``write_run_sidecar``, ``find_run_by_cmd_sha``, ``compute_cmd_sha``).
* :mod:`hpc_agent.state.runtime_prior` — quantile rollups of past task
  runtimes per GPU type.
* :mod:`hpc_agent.state.discover` — scan repo for executor / reducer modules.
* :mod:`hpc_agent.state.user_profiles` — behavioural priors from
  squeue / sacct snapshots.
* :mod:`hpc_agent.state.axes` — ``.hpc/axes.yaml`` reader/writer +
  cold-start axis picker (moved from ``planning/`` in the post-audit
  reorg — planning/ must stay pure, no disk I/O).
* :mod:`hpc_agent.state.stages` — ``.hpc/stages.py`` loader + schema
  validation (moved from ``planning/`` for the same reason — importlib
  of user code is decisively not planning).
"""
