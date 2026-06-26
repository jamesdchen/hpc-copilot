---
name: compute-run-id
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent compute-run-id [--experiment-dir <dir>] --run-name <run_name>
  python: hpc_agent.incorporation.build.compute_run_id.compute_run_id
---

## Purpose

Derive the canonical `(run_id, cmd_sha)` pair from `<experiment_dir>/.hpc/tasks.py`. The run_id is `<run_name>-<sha[:8]>`; the cmd_sha is the full 64-char SHA-256 of the materialized task list. Replaces the inline `python -c "import uuid, hashlib; ..."` (`#200`) — agents now reach for one CLI verb instead of importing `hpc_agent.state.run_sha` directly.

The output also carries two opaque, task-ordered fields materialized in the same load (the framework never interprets either): `trial_tokens` (the reserved `trial_token` each `resolve(i)` returned, or `null` when none) and `trial_params` (the resolved per-task params, with `RESERVED_TASK_KEYS` stripped — the exact `cmd_sha` pre-image). Thread both straight into `write-run-sidecar` so a run's params are recoverable for provenance and re-surface via `prior_records()` (see [campaign-seam](../design/campaign-seam.md)).

## Compose with

- **Predecessors**: `/wrap-entry-point` must have written `.hpc/tasks.py`.
- **Successors**: `find-prior-run --cmd-sha <sha>` (resume check), `build-submit-spec` (pass the `run_id` + `cmd_sha` straight into the spec).

## Notes

- **Pure read**: no filesystem writes. Safe to call repeatedly.
- **Deterministic**: identical `tasks.py` bytes (and resolved kwarg dicts) yield identical output across machines.
- **`run_name` validation**: matches the same `RunIdStrict` regex (`^[A-Za-z0-9._\-]+$`) that the wire layer enforces on submit. Spaces, slashes, anything outside the character class raises `spec_invalid`.
- **Missing `tasks.py`**: raises `spec_invalid` with `/wrap-entry-point` as the suggested remediation.
- **Malformed `tasks.py`**: a tasks.py that loads but whose `total()` / `resolve(i)` raise also surfaces as `spec_invalid`, not an uncaught exception.
