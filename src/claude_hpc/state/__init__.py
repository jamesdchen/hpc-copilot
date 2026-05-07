"""claude_hpc.state — persistent experiment-side metadata.

Read-only views over what previous runs produced (sidecars, runtime
priors, executor inventory). Submodules are deliberately
importer-explicit; reach for the specific submodule:

* :mod:`claude_hpc.state.runs` — per-run sidecars (``read_run_sidecar``,
  ``write_run_sidecar``, ``find_run_by_cmd_sha``, ``compute_cmd_sha``).
* :mod:`claude_hpc.state.runtime_prior` — quantile rollups of past task
  runtimes per GPU type.
* :mod:`claude_hpc.state.discover` — scan repo for executor / reducer modules.
* :mod:`claude_hpc.state.user_profiles` — behavioural priors from
  squeue / sacct snapshots.
"""
