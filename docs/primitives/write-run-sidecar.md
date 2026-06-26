---
name: write-run-sidecar
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/runs/<run_id>.json
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent write-run-sidecar --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.write_run_sidecar.write_run_sidecar
---

## Purpose

Write the per-run sidecar at `<experiment_dir>/.hpc/runs/<run_id>.json` from a `WriteRunSidecarInput` JSON spec. Replaces the inline `python -c "import hpc_agent.state.runs; write_run_sidecar(...)"` call agents used to make to satisfy submit-flow's write-first guard (`#200`) — `_write_first_error` now names this CLI verb instead of pointing the agent at a Python function signature it has to introspect.

Returns `{"path": "<experiment_dir>/.hpc/runs/<run_id>.json"}`.

## Compose with

- **Predecessors**: `compute-run-id` (run_id + cmd_sha), `build-submit-spec` (composable but optional — the sidecar can be written before or after the submit-flow spec is assembled).
- **Successors**: `submit-flow --spec <file>` (the sidecar is its write-first precondition; with this primitive in place, the agent satisfies the precondition explicitly instead of relying on submit-flow's missing-sidecar synthesis path).

## Notes

- **`executor` MUST be the real per-task command** (e.g. `python train.py --seed $SEED`), NOT the job-script dispatcher (`python3 .hpc/_hpc_dispatch.py`). The primitive refuses dispatcher-shaped values at intake — same `_is_runnable_executor` guard that `_ensure_run_sidecar` applies inside submit-flow (`#162`). Shipping a dispatcher-as-executor sidecar would make the dispatcher run itself and the whole array self-recurses.
- **Auto-stamped fields**: `submitted_at` (UTC ISO) and `hpc_agent_version` are filled by the primitive — the agent never sets them.
- **v2 config-snapshot fields**: every field in the wire model that maps to a v2 field (`cluster`, `profile`, `resources`, `env`, `constraints`, `runtime`, `aggregate_defaults`, etc.) is optional. Pass `null` for any that don't apply. The on-disk sidecar omits null v2 fields to keep the file compact.
- **Provenance round-trip (`trial_tokens` / `trial_params`)**: both come from `compute-run-id` (it materializes the task list once at submit). `trial_tokens` is the opaque reconciliation token a closed-loop strategy round-trips; `trial_params` is the resolved per-task params (the `cmd_sha` pre-image, `RESERVED_TASK_KEYS` stripped) — persisting it makes a run's params recoverable, since `cmd_sha` is a one-way hash. Both are recorded verbatim, never interpreted, and re-surfaced by `prior_records()`.
- **`job_ids`**: leave unset at write time — the sidecar is *pending* until submit-flow runs `update_run_sidecar_job_ids` after qsub returns. Setting `job_ids` here on a fresh write is rarely correct (the only legitimate case is replaying an externally-tracked submission).
- **Idempotent**: re-running with the same spec produces the same on-disk bytes (modulo the auto-stamped `submitted_at`). The primitive's idempotency-key is `run_id`; the journal layer dedupes by it.
- **`wave_map` auto-derivation**: when `wave_map` is null and `.hpc/axes.yaml` carries a full axis enumeration, the underlying `write_run_sidecar` function derives the assignment (see `state/runs.py` for the warm-then-cold picker). Pass an explicit `wave_map` only when the auto-derivation doesn't fit your campaign shape.
