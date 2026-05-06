---
name: read-runtime-prior
verb: query
inputs:
- name: profile
  type: string
- name: cluster
  type: string
- name: cmd_sha
  type: string
  description: Filter samples to one cmd_sha (recommended after .hpc/tasks.py edits).
  default: null
- name: experiment_dir
  type: path
  default: cwd
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-mapreduce runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]
  python: claude_hpc.state.runtime_prior.roll_up_quantiles
exit_codes:
- 0: ok
- 1: spec_invalid
- 3: internal
---
# read-runtime-prior

> **Internal primitive.** Forecasting helper consumed by
> `score-submit-plan`'s walltime-selection step. Direct invocation
> is fine for ad-hoc analysis but the typical hot path goes
> through `plan-submit`.

Roll up `<repo>/.hpc/runtimes/<profile>.<cluster>.json` samples
into per-`gpu_type` quantile distributions (p50/p95/p99). Drives
`score-submit-plan`'s walltime selection. Read-only, local — no
SSH.

## Composers

- `score-submit-plan` (the planner consults the prior to choose
  walltime per candidate constraint).
- Calibration tooling (`house-edge`, `walltime-drift`) reads the
  same files via `read_samples`; this atom is a higher-level
  rollup specifically for the planner.

## Invariants

- **Pure read.** No SSH, no journal mutation. Reads only the
  per-`(profile, cluster)` runtime ledger.
- **`needs_canary` is the cold-start signal.** When no
  qualifying samples exist for ANY `gpu_type` after filtering,
  `needs_canary=True` and `quantiles={}`. Caller's contract:
  submit a 1-task canary, ingest its result via
  `runtime_prior.append_sample`, then re-call.
- **Quantiles are per-`gpu_type` independently.** A profile
  whose samples span multiple GPU types reports each separately;
  the planner picks the constraint with the tightest quantiles
  for the predicted candidate.

## Coupling

- The `_RuntimeQuantiles` shape in
  `_schema_models/runtime_prior.py` is the wire contract
  (p50/p95/p99/mean_sec/n_samples/min_sec/max_sec). Adding a
  quantile means extending the model and re-checking the
  planner's walltime selector.
- The runtime-ledger filename convention
  (`runtimes/<profile>.<cluster>.json`) is the storage contract.
  Renaming the dir or filename pattern requires `read_samples`,
  `append_sample`, and the cluster-side reporter all in lockstep.

## Failure modes

- Edited `.hpc/tasks.py` produces a different `cmd_sha`; old
  samples are misleading. Caller should pass `cmd_sha=<new>` to
  filter — but the framework doesn't enforce this; cold-start is
  the silent fallback.
- Corrupt or partial runtime-ledger JSON → `read_samples`
  silently returns `[]` (better to cold-start than to crash on a
  log-rotation race).
