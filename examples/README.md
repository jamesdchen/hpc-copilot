# Examples

Standalone artifacts that are **not** part of the installed `hpc-agent`
package (excluded from packaging in `pyproject.toml`; outside every
core lint's scan root). They demonstrate integration surfaces without
adding core dependencies.

- [`crowd-compute-executor/`](crowd-compute-executor/) — a
  containerized executor that honors the dispatcher env contract
  (`HPC_KW_*`, `RESULT_DIR`, `HPC_TASK_ID`) with zero hpc-agent
  imports, so the same image runs under a SLURM dispatcher or a
  crowd-compute platform (Vast.ai / SaladCloud / Akash).
- [`plugins/hpc-agent-vastai/`](plugins/hpc-agent-vastai/) — a
  skeleton plugin distribution showing how a crowd-compute backend
  registers through the `hpc_agent.plugins` entry-point group and the
  `HPCBackend` registry. All compute methods are documented stubs.

Background and seam analysis:
[`docs/proposals/crowd-compute-backend.md`](../docs/proposals/crowd-compute-backend.md).
