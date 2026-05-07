---
name: predict-start-time
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent predict-start-time --spec <path>
  python: claude_hpc.atoms.predict_start_time.predict_start_time_primitive
---
# predict-start-time

Given the queue state and your job parameters, predict when your job would start under each candidate submit offset (0 hours now, 1 hour, 3 hours, 6 hours, etc.). The primitive runs a SLURM backfill simulator informed by the floor-estimator and an optional LightGBM residual predictor; it returns the offset that minimizes expected total time to start plus returns the full sweep for transparency.

## Inputs

- `now_iso` (string) — ISO timestamp anchoring the forecast.
- `squeue_text` (string) — Raw output of `squeue --user='*' -O 'JOBID|PRIORITY|PARTITION|USERNAME|STATE|TIME_LEFT|TIME_LIMIT'`. The primitive parses this to build the queue-state model.
- `partition` (string) — Name of the partition you intend to submit to.
- `partition_slot_count` (integer) — Number of available compute slots in that partition.
- `your_priority` (integer) — Your job's priority score once queued (from `scontrol show job`).
- `your_walltime_sec` (integer) — Requested wall-time in seconds.
- `your_user` (string, optional) — Your username for fairshare lookups.
- `your_constraint` (string, default `""`) — SLURM features constraint your job requires (e.g., `"gpu:a100"`).
- `sshare_text` (string, optional) — Raw output of `sshare -P`. When absent, fairshare features collapse to sentinels.
- `pending_walltime_default_sec` (integer, default 86400) — Default estimated wall-time for jobs in pending state when the actual time-limit is unknown.
- `candidate_offsets_hours` (list of floats, default `[0.0, 1.0, 3.0, 6.0, 12.0, 24.0]`) — Offsets to evaluate in the sweep.
- `model_path` (string, optional) — Path to a serialized LightGBM `model.txt`. When absent, predictions fall back to the pessimistic floor.

## Outputs

A `PredictStartTimeResult` object with:

- `best_submit_offset_hours` (float) — The offset that minimizes total time (the primitive's recommendation).
- `best_predicted_start_iso` (string) — Predicted absolute start time under the best offset.
- `best_total_time_sec` (integer) — Total seconds from now until predicted start under the best offset.
- `candidates` (list) — Full sweep results, one entry per requested offset. Each entry includes `offset_hours`, `predicted_iso`, `floor_pessimistic_iso`, `floor_optimistic_iso`, `overhead_sec`, `total_time_sec`, and `method` (the forecast technique: `"floor"` or `"lgbm_residual"`).

## Errors

None declared on the primitive. Spec validation errors raise `pydantic.ValidationError`; malformed `squeue_text` / `sshare_text` and missing `model_path` propagate as Python exceptions through the CLI envelope's `internal` category.

## Idempotency

The primitive is pure local — it reads the text you pass in and returns a deterministic forecast. Calling twice with the same inputs produces the same output.

## Notes

- The primitive does not make SSH calls; the slash command handles all cluster I/O and passes the raw text in. This keeps the framework boundary side-effect-free.
- When `model_path` is absent or unreadable, the primitive falls back to the floor predictor (pessimistic + optimistic bounds from queueing theory).
- The sweep is transparent: returning the full `candidates` list lets the agent see the confidence across offsets (e.g., if multiple offsets score similarly, waiting has less downside risk).
- The spec uses `extra="forbid"` — passing an unknown field raises `pydantic.ValidationError` rather than silently ignoring it.

**Schemas:** [`predict_start_time.input.json`](../../src/claude_hpc/schemas/predict_start_time.input.json), [`predict_start_time.output.json`](../../src/claude_hpc/schemas/predict_start_time.output.json).
