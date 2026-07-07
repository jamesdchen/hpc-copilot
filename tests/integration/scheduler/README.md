# tests/integration/scheduler — real Slurm, in a container

One smoke test (`test_scheduler_smoke.py`) drives the framework's real submit
spine — `submit_flow → monitor_flow → aggregate_flow` — against a real
single-node Slurm running in a container, over SSH. No mocks on the transport or
scheduler seam. It converts the "found live in proving run #N" class of bug into
"found in CI".

## It is inert unless you opt in

The test is guarded by a module-level `skipif`: without `HPC_SCHEDULER_IT=1`
(and the container it points at) it skips cleanly, so the main pytest matrix and
local dev collect it but never run it. It is also marked `slow` — the top-level
`tests/conftest.py` shadows the real `ssh`/`rsync` binaries for non-`slow`
tests, and this tier deliberately reaches a real cluster.

Markers: `scheduler_integration` (this tier's selector, registered in the local
`conftest.py`, NOT in `pyproject.toml`) and `slow`.

## Collect-only (proves it stays inert)

```
.venv/Scripts/python.exe -m pytest tests/integration/scheduler --collect-only -q
```

## Running it for real

The whole harness lives in `ci/slurm/` (container) and
`.github/workflows/scheduler-integration.yml` (the CI job). The local-repro
docker recipe and the design rationale — including what it deliberately does NOT
cover (SGE) — are in
[`docs/internals/scheduler-integration-ci.md`](../../../docs/internals/scheduler-integration-ci.md).

Env vars the test reads (all set by the workflow):

| var | meaning | default |
| --- | --- | --- |
| `HPC_SCHEDULER_IT` | must be `1` to run | — (skips) |
| `HPC_CLUSTERS_CONFIG` | points the framework at the container's `clusters.yaml` | — |
| `HPC_SCHEDULER_IT_SSH_TARGET` | ssh target/alias for the login node | `hpcuser@slurmci` |
| `HPC_SCHEDULER_IT_CLUSTER` | cluster name in `clusters.yaml` | `slurmci` |
| `HPC_SCHEDULER_IT_REMOTE_BASE` | remote scratch base for staged runs | `/home/hpcuser/scratch` |
