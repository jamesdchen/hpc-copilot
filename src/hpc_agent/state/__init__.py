"""hpc_agent.state — persistent experiment-side metadata.

Read-only views over what previous runs produced (sidecars, runtime
priors, executor inventory). Submodules are deliberately
importer-explicit; reach for the specific submodule:

* :mod:`hpc_agent.state.runs` — per-run sidecars (``read_run_sidecar``,
  ``write_run_sidecar``, ``find_run_by_cmd_sha``, ``compute_cmd_sha``).
* :mod:`hpc_agent.state.runtime_prior` — quantile rollups of past task
  runtimes per GPU type.
* :mod:`hpc_agent.state.discover` — scan repo for executor / reducer modules.
* :mod:`hpc_agent.state.user_profiles` — behavioural priors from
  squeue / sacct snapshots.
"""
