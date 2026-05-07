---
name: update-run-constraints
verb: mutate
side_effects:
- ssh: <cluster> (scontrol update Features)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-mapreduce update-run-constraints --spec <path>
  python: claude_hpc.runner.update_constraints.update_run_constraints
---
# update-run-constraints

Update a submitted run's SLURM Features constraint (e.g., `gpu:a100|gpu:l40s`) via `scontrol update jobid=<id> Features=<expr>` without re-submitting and losing age priority. Lesson 9: `scontrol update` works post-submit and preserves accumulated priority, making it safer than cancel-and-resubmit. Pass either `set_features` (replace the entire Features expression) or `add_features` (extend with new constraints), but not both.

## Inputs

- `run_id` (string) — Run ID to update.
- `set_features` (list of strings, optional) — Replace the entire Features expression with this set. Mutually exclusive with `add_features`.
- `add_features` (list of strings, default `[]`) — Features to add to the existing set (de-duplicated, joined with `|`).

## Outputs

An `UpdateRunConstraintsResult` object with:

- `run_id` (string) — The run ID updated.
- `job_ids_updated` (list of strings) — Job IDs that successfully updated on the cluster.
- `job_ids_failed` (list of strings) — Job IDs that failed to update (SSH unreachable, scontrol error, etc.).
- `new_features` (list of strings) — The final Features set applied.

## Errors

- `spec_invalid` — Spec validation failed (e.g., neither `set_features` nor `add_features` provided, or both provided). User error; not retry-safe.
- `ssh_unreachable` — SSH connection to the cluster failed. Retry-safe; network may recover.
- `remote_command_failed` — `scontrol update` returned non-zero on the cluster. Not retry-safe (likely a semantic error, e.g., job already completed). Check job state before retrying.

## Idempotency

Idempotency key: `run_id`. Re-running with the same target Features set produces the same on-cluster state. If some jobs have already been updated and others haven't, re-running updates the remaining jobs without duplicating the operation on already-updated jobs.

## Notes

- The primitive reads the run's sidecar to fetch `job_ids` and `ssh_target`. If the sidecar is missing `ssh_target` (v1 sidecars), the primitive raises `spec_invalid` rather than guessing a routing target.
- Features are joined with the SLURM OR operator `|` (any-of). A future extension might add `set_operator` to the spec to support AND (`&`) semantics, but the lesson-9 use case (add fallback GPUs) requires OR.
- Feature names are validated against `[A-Za-z0-9._-]` to defend against shell injection via the scontrol command.
- The sidecar's recorded Features are updated after the cluster-side operation succeeds (best-effort); if the sidecar write fails, the cluster-side update has already succeeded for the affected jobs.
